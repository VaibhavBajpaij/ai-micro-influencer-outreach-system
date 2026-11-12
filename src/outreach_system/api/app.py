"""FastAPI application exposing authenticated pipeline triggers, review endpoints, and dashboard UI."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from outreach_system.config import NICHE_PRESETS, Settings
from outreach_system.models import (
    DMStatus,
    EmailSendStatus,
    FilterStatus,
    resolve_generation_mode,
)
from outreach_system.personalization import LLMClient
from outreach_system.pipeline import OutreachPipeline
from outreach_system.sending import EmailOutreachSender, InstagramDMWorkflow, is_sendable_email
from outreach_system.storage import OutreachDatabase

STATIC_DIR = Path(__file__).resolve().parent / "static"


class RunPipelineRequest(BaseModel):
    niche: str = "technology"
    target_count: int = Field(default=85, ge=1, le=200)
    send_mode: Literal["simulate", "smtp"] = "simulate"
    auto_send: bool = False
    simulate_dms: bool = False


class RunBatchedPipelineRequest(BaseModel):
    niche: str = "technology"
    total_target: int = Field(default=250, ge=1, le=1000)
    batch_size: int = Field(default=100, ge=1, le=200)
    send_mode: Literal["simulate", "smtp"] = "simulate"
    simulate_dms: bool = True
    force_regenerate: bool = False


class SendOutreachRequest(BaseModel):
    mode: Literal["simulate", "smtp"] = "simulate"
    influencer_ids: list[str]


class BulkSendOutreachRequest(BaseModel):
    mode: Literal["simulate", "smtp"] = "simulate"
    confirm_bulk: bool = False


class VerifySmtpRequest(BaseModel):
    delivered: bool
    notes: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    shared_db = OutreachDatabase(
        cfg.resolve_database_path(),
        stale_sending_timeout_seconds=cfg.stale_sending_timeout_seconds,
    )
    app = FastAPI(
        title="Automated Micro-Influencer Outreach System",
        version="1.2.0",
        description="Discovery, Filtering, Profile Enrichment, AI Personalization & Sending API",
    )

    def get_db() -> OutreachDatabase:
        return shared_db

    def get_pipeline() -> OutreachPipeline:
        return OutreachPipeline(cfg, db=shared_db)

    def require_api_auth(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> None:
        expected = cfg.api_auth_token.strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="API authentication token is not configured on server (set API_AUTH_TOKEN or OUTREACH_API_KEY).",
            )
        provided = (x_api_key or "").strip()
        if not provided and authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer":
                provided = token.strip()
        if not provided or not secrets.compare_digest(provided, expected):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing API authentication token.",
            )

    def ensure_smtp_authorized(mode: str) -> None:
        if mode == "smtp" and not cfg.allow_live_smtp:
            raise HTTPException(
                status_code=403,
                detail="Live SMTP sending is disabled (set ALLOW_LIVE_SMTP=true in server configuration).",
            )

    @app.get("/", include_in_schema=False)
    def serve_dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict:
        llm = LLMClient(cfg)
        provider, model = await llm.resolve_provider()
        return {
            "status": "ok",
            "brand": cfg.brand_name,
            "default_niche": cfg.default_niche,
            "generation_mode": resolve_generation_mode(provider),
            "llm_provider": provider,
            "llm_model": model,
            "send_mode": cfg.send_mode,
            "auth_configured": bool(cfg.api_auth_token.strip()),
            "allow_live_smtp": cfg.allow_live_smtp,
            "available_niches": [
                {"key": k, "name": v.display_name} for k, v in NICHE_PRESETS.items()
            ],
        }

    @app.get("/api/summary", dependencies=[Depends(require_api_auth)])
    async def summary() -> dict:
        db = get_db()
        current_run_id = db.get_current_run_id()
        all_profiles = db.get_influencers(current_only=True)
        qualified = [p for p in all_profiles if p.status == FilterStatus.QUALIFIED]
        rejected = [p for p in all_profiles if p.status == FilterStatus.REJECTED]
        emails_found = [p for p in all_profiles if is_sendable_email(p.contact_email)]
        outreach_ready = [p for p in qualified if is_sendable_email(p.contact_email)]

        current_messages = db.get_messages(current_only=True)
        historical_messages = db.get_messages(current_only=False)
        run_logs = (
            db.get_outreach_log(run_id=current_run_id)
            if current_run_id
            else db.get_outreach_log()
        )
        all_logs = db.get_outreach_log()
        dms = db.get_dm_queue(current_only=True)
        llm = LLMClient(cfg)
        provider, model = await llm.resolve_provider()
        latest_run = db.get_latest_run()

        return {
            "current_run_id": current_run_id,
            "discovered_count": len(all_profiles),
            "qualified_count": len(qualified),
            "outreach_ready_count": len(outreach_ready),
            "qualified_missing_email_count": len(qualified) - len(outreach_ready),
            "rejected_count": len(rejected),
            "emails_found_count": len(emails_found),
            "qualified_emails_count": len(outreach_ready),
            "messages_generated_count": len(current_messages),
            "messages_llm_count": sum(1 for m in current_messages if m.generation_mode == "LLM"),
            "messages_fallback_count": sum(1 for m in current_messages if m.generation_mode != "LLM"),
            "historical_messages_count": len(historical_messages),
            "emails_sent_count": sum(1 for row in run_logs if row.sent),
            "emails_skipped_no_email": sum(
                1 for row in run_logs if row.status == EmailSendStatus.SKIPPED_NO_EMAIL
            ),
            "emails_skipped_duplicate": sum(
                1
                for row in run_logs
                if row.status
                in (
                    EmailSendStatus.SKIPPED_DUPLICATE,
                    EmailSendStatus.SKIPPED_VERIFICATION_PENDING,
                )
            ),
            "emails_verification_pending": sum(
                1
                for row in run_logs
                if row.status == EmailSendStatus.MANUAL_VERIFICATION_REQUIRED
            ),
            "emails_failed_count": sum(
                1 for row in run_logs if row.status == EmailSendStatus.FAILED
            ),
            "historical_emails_sent_count": sum(1 for row in all_logs if row.sent),
            "dm_queued_count": len(dms),
            "dm_sent_count": sum(1 for d in dms if d.sent),
            "dm_skipped_no_handle": sum(1 for d in dms if d.status == DMStatus.SKIPPED_NO_HANDLE),
            "dm_pending_count": sum(1 for d in dms if d.status == DMStatus.PENDING_MANUAL),
            "generation_mode": resolve_generation_mode(provider),
            "llm_provider": provider,
            "llm_model": model,
            "send_mode": cfg.send_mode,
            "allow_live_smtp": cfg.allow_live_smtp,
            "latest_run": latest_run.model_dump() if latest_run else None,
        }

    @app.get("/api/influencers", dependencies=[Depends(require_api_auth)])
    def list_influencers(
        status: str | None = Query(default=None),
        search: str | None = Query(default=None),
        current_only: bool = Query(default=True),
    ) -> list[dict]:
        db = get_db()
        filter_status = None
        if status in (FilterStatus.QUALIFIED.value, FilterStatus.REJECTED.value):
            filter_status = FilterStatus(status)
        items = db.get_influencers(status=filter_status, current_only=current_only)
        if search:
            q = search.strip().lower()
            items = [
                p
                for p in items
                if q in p.name.lower()
                or q in p.platform.lower()
                or q in p.contact_email.lower()
                or q in " ".join(p.content_themes).lower()
                or q in p.tier.lower()
                or q in p.outreach_readiness.lower()
            ]
        return [p.model_dump() for p in items]

    @app.get("/api/messages", dependencies=[Depends(require_api_auth)])
    def list_messages(current_only: bool = Query(default=True)) -> list[dict]:
        db = get_db()
        return [m.model_dump() for m in db.get_messages(current_only=current_only)]

    @app.get("/api/messages/history", dependencies=[Depends(require_api_auth)])
    def list_message_history(run_id: str | None = Query(default=None)) -> list[dict]:
        db = get_db()
        return [m.model_dump() for m in db.get_messages(current_only=False, run_id=run_id)]

    @app.get("/api/outreach-log", dependencies=[Depends(require_api_auth)])
    def list_outreach_log(
        run_id: str | None = Query(default=None),
        current_only: bool = Query(default=True),
    ) -> list[dict]:
        db = get_db()
        if run_id is not None:
            rows = db.get_outreach_log(run_id=run_id)
        elif current_only:
            active_run = db.get_current_run_id()
            rows = db.get_outreach_log(run_id=active_run) if active_run else db.get_outreach_log()
        else:
            rows = db.get_outreach_log()
        return [r.model_dump() for r in rows]

    @app.get("/api/dm-queue", dependencies=[Depends(require_api_auth)])
    def list_dm_queue(current_only: bool = Query(default=True)) -> list[dict]:
        db = get_db()
        return [d.model_dump() for d in db.get_dm_queue(current_only=current_only)]

    @app.post("/api/pipeline/run", dependencies=[Depends(require_api_auth)])
    async def run_pipeline(req: RunPipelineRequest) -> dict:
        if req.niche.strip().lower() not in NICHE_PRESETS:
            raise HTTPException(status_code=400, detail=f"Unsupported niche: {req.niche}")
        if req.auto_send:
            ensure_smtp_authorized(req.send_mode)
            pipeline = get_pipeline()
            result = await pipeline.run_full_pipeline(
                niche=req.niche,
                target_count=req.target_count,
                send_mode=req.send_mode,
                simulate_dms=req.simulate_dms,
                force_regenerate=True,
            )
            return result.model_dump()

        pipeline = get_pipeline()
        profiles = await pipeline.run_discovery_filter_enrich(
            niche=req.niche, target_count=req.target_count
        )
        messages = await pipeline.run_personalization(force=True)
        qualified = [p for p in profiles if p.status == FilterStatus.QUALIFIED]
        outreach_ready = [p for p in qualified if is_sendable_email(p.contact_email)]
        return {
            "run_id": profiles[0].run_id if profiles else "",
            "niche": NICHE_PRESETS[req.niche.strip().lower()].display_name,
            "discovered_count": len(profiles),
            "qualified_count": len(qualified),
            "outreach_ready_count": len(outreach_ready),
            "messages_generated_count": len(messages),
        }

    @app.post("/api/pipeline/run-batch", dependencies=[Depends(require_api_auth)])
    async def run_pipeline_batch(req: RunBatchedPipelineRequest) -> dict:
        if req.niche.strip().lower() not in NICHE_PRESETS:
            raise HTTPException(status_code=400, detail=f"Unsupported niche: {req.niche}")
        ensure_smtp_authorized(req.send_mode)
        pipeline = get_pipeline()
        result = await pipeline.run_batched_pipeline(
            niche=req.niche,
            total_target=req.total_target,
            batch_size=req.batch_size,
            send_mode=req.send_mode,
            simulate_dms=req.simulate_dms,
            force_regenerate=req.force_regenerate,
        )
        return result.model_dump()

    @app.post("/api/personalize/regenerate", dependencies=[Depends(require_api_auth)])
    async def regenerate_messages() -> dict:
        pipeline = get_pipeline()
        messages = await pipeline.run_personalization(force=True)
        return {"messages_generated_count": len(messages)}

    @app.post("/api/outreach/send", dependencies=[Depends(require_api_auth)])
    def send_outreach(req: SendOutreachRequest) -> dict:
        cleaned_ids = [cid.strip() for cid in req.influencer_ids if cid and cid.strip()]
        if not cleaned_ids:
            raise HTTPException(
                status_code=400,
                detail="influencer_ids must not be empty; provide specific creator IDs or call /api/outreach/send-bulk.",
            )
        ensure_smtp_authorized(req.mode)

        pipeline = get_pipeline()
        db = pipeline.db
        shortlisted = db.get_influencers(status=FilterStatus.QUALIFIED, current_only=True)
        shortlisted_ids = {p.id for p in shortlisted}
        unknown_ids = [cid for cid in cleaned_ids if cid not in shortlisted_ids]
        if unknown_ids:
            missing_in_db = [cid for cid in unknown_ids if db.get_influencer(cid) is None]
            if missing_in_db:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown influencer_id(s): {', '.join(missing_in_db)}",
                )
            raise HTTPException(
                status_code=400,
                detail=f"Influencer_id(s) not in current qualified shortlist: {', '.join(unknown_ids)}",
            )

        sender = EmailOutreachSender(cfg, db)
        entries = sender.dispatch_all(
            shortlisted, mode_override=req.mode, influencer_ids=set(cleaned_ids)
        )
        pipeline.export_all_artifacts()
        return {
            "processed": len(entries),
            "sent": sum(1 for e in entries if e.sent),
            "entries": [e.model_dump() for e in entries],
        }

    @app.post("/api/outreach/send-bulk", dependencies=[Depends(require_api_auth)])
    def send_outreach_bulk(req: BulkSendOutreachRequest) -> dict:
        if not req.confirm_bulk:
            raise HTTPException(
                status_code=400,
                detail="confirm_bulk must be true to dispatch outreach to all current qualified creators.",
            )
        ensure_smtp_authorized(req.mode)

        pipeline = get_pipeline()
        db = pipeline.db
        shortlisted = db.get_influencers(status=FilterStatus.QUALIFIED, current_only=True)

        sender = EmailOutreachSender(cfg, db)
        entries = sender.dispatch_all(shortlisted, mode_override=req.mode, influencer_ids=None)
        pipeline.export_all_artifacts()
        return {
            "processed": len(entries),
            "sent": sum(1 for e in entries if e.sent),
            "entries": [e.model_dump() for e in entries],
        }

    @app.post("/api/outreach/{log_id}/verify-smtp", dependencies=[Depends(require_api_auth)])
    def verify_uncertain_smtp(log_id: int, req: VerifySmtpRequest) -> dict:
        pipeline = get_pipeline()
        existing = pipeline.db.get_outreach_entry(log_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Outreach log entry {log_id} not found")
        try:
            updated = pipeline.db.confirm_uncertain_smtp_outcome(
                log_id,
                delivered=req.delivered,
                operator_note=req.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        pipeline.export_all_artifacts()
        return updated.model_dump()

    @app.post("/api/dm/{influencer_id}/simulate", dependencies=[Depends(require_api_auth)])
    def simulate_dm(influencer_id: str) -> dict:
        pipeline = get_pipeline()
        wf = InstagramDMWorkflow(pipeline.db)
        entry = wf.simulate_send(influencer_id)
        if not entry:
            raise HTTPException(status_code=404, detail="DM queue entry not found")
        pipeline.export_all_artifacts()
        return entry.model_dump()

    @app.post("/api/dm/{influencer_id}/mark-sent", dependencies=[Depends(require_api_auth)])
    def mark_dm_sent(influencer_id: str) -> dict:
        pipeline = get_pipeline()
        wf = InstagramDMWorkflow(pipeline.db)
        entry = wf.mark_sent_manually(influencer_id)
        if not entry:
            raise HTTPException(status_code=404, detail="DM queue entry not found")
        pipeline.export_all_artifacts()
        return entry.model_dump()

    return app


app = create_app()
