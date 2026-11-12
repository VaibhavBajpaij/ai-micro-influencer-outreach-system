"""End-to-end automated micro-influencer discovery, filtering, enrichment, personalization, and sending pipeline."""

from __future__ import annotations

import asyncio
import csv
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from outreach_system.config import Settings, get_niche_preset
from outreach_system.discovery import BlueskyDiscoverer, YouTubeDiscoverer
from outreach_system.enrichment import ProfileEnricher, annotate_cross_platform_identities
from outreach_system.filtering import InfluencerClassifier
from outreach_system.models import (
    DMStatus,
    EmailSendStatus,
    FilterStatus,
    InfluencerProfile,
    InfluencerRaw,
    PersonalizedMessage,
    PipelineSummary,
    resolve_generation_mode,
)
from outreach_system.personalization import MessagePersonalizer
from outreach_system.sending import (
    EmailOutreachSender,
    InstagramDMWorkflow,
    is_sendable_email,
    validate_send_mode,
)
from outreach_system.storage import OutreachDatabase


class OutreachPipeline:
    """Coordinates every stage of the micro-influencer outreach workflow."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db: OutreachDatabase | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.db = db or OutreachDatabase(
            self.settings.resolve_database_path(),
            stale_sending_timeout_seconds=self.settings.stale_sending_timeout_seconds,
        )
        self.outputs_dir = self.settings.resolve_outputs_dir()

    async def _discover_raw(self, preset, total_target: int) -> list[InfluencerRaw]:
        """Discover creators across YouTube and Bluesky and deduplicate canonical profile URLs."""
        yt_target = max(1, int(total_target * 0.78))
        bsky_target = max(0, total_target - yt_target)

        yt = YouTubeDiscoverer()
        bsky = BlueskyDiscoverer()

        if bsky_target > 0:
            yt_raw, bsky_raw = await asyncio.gather(
                yt.discover(preset, limit=yt_target),
                bsky.discover(preset, limit=bsky_target),
            )
        else:
            yt_raw = await yt.discover(preset, limit=yt_target)
            bsky_raw = []

        merged_raw: list[InfluencerRaw] = []
        seen_urls: set[str] = set()
        for item in [*yt_raw, *bsky_raw]:
            key = item.profile_url.lower().rstrip("/")
            if key not in seen_urls:
                seen_urls.add(key)
                merged_raw.append(item)
        return merged_raw

    async def _evaluate_and_enrich(
        self,
        raw_items: list[InfluencerRaw],
        preset,
        active_run_id: str,
    ) -> list[InfluencerProfile]:
        """Run rule-based classification, enrichment, and cross-platform tagging for a batch."""
        classifier = InfluencerClassifier(self.settings, preset)
        evaluated = [(raw, classifier.evaluate(raw)) for raw in raw_items]

        enricher = ProfileEnricher(preset)
        profiles = await enricher.enrich_all(evaluated, crawl_websites_for_rejected=False)
        profiles = annotate_cross_platform_identities(profiles)
        for profile in profiles:
            profile.run_id = active_run_id
        return profiles

    async def run_discovery_filter_enrich(
        self,
        niche: str | None = None,
        target_count: int | None = None,
        run_id: str | None = None,
    ) -> list[InfluencerProfile]:
        """Stage 1-3: Discover 50+ creators, classify pass/fail, and enrich profiles."""
        active_run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        preset = get_niche_preset(niche or self.settings.default_niche)
        total_target = target_count or self.settings.target_discovery_count

        merged_raw = await self._discover_raw(preset, total_target)
        profiles = await self._evaluate_and_enrich(merged_raw, preset, active_run_id)

        self.db.upsert_influencers(profiles, run_id=active_run_id, mark_previous_inactive=True)
        self.export_all_artifacts()
        return profiles

    async def run_personalization(
        self,
        force: bool = False,
        run_id: str | None = None,
        batch_size: int = 200,
    ) -> list[PersonalizedMessage]:
        """Stage 4: Generate 60-90w Email Pitches and 15-30w Instagram DMs strictly for current qualified creators."""
        active_run_id = run_id or self.db.get_current_run_id() or f"run-{uuid.uuid4().hex[:10]}"
        shortlisted = self.db.get_influencers(status=FilterStatus.QUALIFIED, current_only=True)
        shortlisted_ids = {p.id for p in shortlisted}

        for profile in shortlisted:
            if not profile.run_id:
                profile.run_id = active_run_id

        existing_current = {
            m.influencer_id: m
            for m in self.db.get_messages(current_only=True)
            if m.influencer_id in shortlisted_ids
        }

        if force:
            targets = shortlisted
            preserved: list[PersonalizedMessage] = []
        else:
            targets = [p for p in shortlisted if p.id not in existing_current]
            preserved = [existing_current[p.id] for p in shortlisted if p.id in existing_current]

        generated: list[PersonalizedMessage] = []
        if targets:
            personalizer = MessagePersonalizer(self.settings)
            chunk_sz = max(1, batch_size)
            for idx in range(0, len(targets), chunk_sz):
                chunk_msgs = await personalizer.generate_for_shortlist(targets[idx : idx + chunk_sz])
                for msg in chunk_msgs:
                    msg.run_id = active_run_id
                generated.extend(chunk_msgs)

        current_messages = [*preserved, *generated]
        for msg in current_messages:
            msg.run_id = active_run_id

        self.db.save_run_messages(current_messages, run_id=active_run_id)
        stored_messages = self.db.get_messages(current_only=True)

        dm_workflow = InstagramDMWorkflow(self.db)
        dm_workflow.sync_queue(stored_messages, run_id=active_run_id)
        self.export_all_artifacts()
        return stored_messages

    def run_sending(
        self,
        mode: str | None = None,
        simulate_dms: bool = False,
        run_id: str | None = None,
    ) -> dict:
        """Stage 5: Dispatch/simulate emails (with atomic duplicate prevention) and optionally simulate DMs."""
        resolved_mode = validate_send_mode(mode, self.settings.send_mode)
        active_run_id = run_id or self.db.get_current_run_id()
        shortlisted = self.db.get_influencers(status=FilterStatus.QUALIFIED, current_only=True)
        sender = EmailOutreachSender(self.settings, self.db)
        email_entries = sender.dispatch_all(
            shortlisted, mode_override=resolved_mode, run_id=active_run_id
        )

        dm_workflow = InstagramDMWorkflow(self.db)
        dm_entries = self.db.get_dm_queue(current_only=True)
        if simulate_dms:
            for dm in dm_entries:
                if not dm.sent and dm.status != DMStatus.SKIPPED_NO_HANDLE:
                    dm_workflow.simulate_send(dm.influencer_id)
            dm_entries = self.db.get_dm_queue(current_only=True)

        self.export_all_artifacts()
        return {"email_entries": email_entries, "dm_entries": dm_entries}

    def _build_summary(
        self,
        *,
        run_id: str,
        niche_key: str,
        profiles: list[InfluencerProfile],
        messages: list[PersonalizedMessage],
        dispatch_res: dict,
    ) -> PipelineSummary:
        qualified = [p for p in profiles if p.status == FilterStatus.QUALIFIED]
        rejected = [p for p in profiles if p.status == FilterStatus.REJECTED]
        emails_found = [p for p in profiles if is_sendable_email(p.contact_email)]
        outreach_ready = [p for p in qualified if is_sendable_email(p.contact_email)]

        email_entries = dispatch_res["email_entries"]
        sent_cnt = sum(1 for e in email_entries if e.sent)
        no_email_cnt = sum(
            1 for e in email_entries if e.status == EmailSendStatus.SKIPPED_NO_EMAIL
        )
        dup_cnt = sum(
            1
            for e in email_entries
            if e.status
            in (
                EmailSendStatus.SKIPPED_DUPLICATE,
                EmailSendStatus.SKIPPED_VERIFICATION_PENDING,
            )
        )

        provider_used = messages[0].llm_provider if messages else "none"
        model_used = messages[0].llm_model if messages else "none"
        generation_mode = (
            messages[0].generation_mode if messages else resolve_generation_mode(provider_used)
        )

        return PipelineSummary(
            run_id=run_id,
            niche=get_niche_preset(niche_key).display_name,
            discovered_count=len(profiles),
            qualified_count=len(qualified),
            outreach_ready_count=len(outreach_ready),
            qualified_missing_email_count=len(qualified) - len(outreach_ready),
            rejected_count=len(rejected),
            emails_found_count=len(emails_found),
            messages_generated_count=len(messages),
            emails_sent_count=sent_cnt,
            emails_skipped_no_email=no_email_cnt,
            emails_skipped_duplicate=dup_cnt,
            dm_queued_count=len(dispatch_res["dm_entries"]),
            dm_sent_count=sum(1 for d in dispatch_res["dm_entries"] if d.sent),
            dm_skipped_no_handle=sum(
                1 for d in dispatch_res["dm_entries"] if d.status == DMStatus.SKIPPED_NO_HANDLE
            ),
            generation_mode=generation_mode,
            llm_provider_used=provider_used,
            llm_model_used=model_used,
            executed_at=datetime.now(UTC).isoformat(),
        )

    async def run_full_pipeline(
        self,
        niche: str | None = None,
        target_count: int | None = None,
        send_mode: str | None = None,
        simulate_dms: bool = True,
        force_regenerate: bool = True,
    ) -> PipelineSummary:
        """Execute Discovery -> Filtering -> Enrichment -> AI Personalization -> Sending -> Export."""
        resolved_mode = validate_send_mode(send_mode, self.settings.send_mode)
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        niche_key = niche or self.settings.default_niche

        profiles = await self.run_discovery_filter_enrich(
            niche=niche_key, target_count=target_count, run_id=run_id
        )
        messages = await self.run_personalization(force=force_regenerate, run_id=run_id)
        dispatch_res = self.run_sending(
            mode=resolved_mode, simulate_dms=simulate_dms, run_id=run_id
        )
        summary = self._build_summary(
            run_id=run_id,
            niche_key=niche_key,
            profiles=profiles,
            messages=messages,
            dispatch_res=dispatch_res,
        )
        self.db.save_pipeline_run(summary)
        self.export_all_artifacts()
        return summary

    async def run_batched_pipeline(
        self,
        *,
        niche: str | None = None,
        total_target: int = 500,
        batch_size: int = 100,
        send_mode: str | None = None,
        simulate_dms: bool = True,
        force_regenerate: bool = False,
        max_total: int = 1000,
    ) -> PipelineSummary:
        """Execute 500+ scale discovery/enrichment/personalization/outreach in bounded batches under one run_id."""
        if not 1 <= batch_size <= 200:
            raise ValueError("batch_size must be between 1 and 200")
        if not 1 <= total_target <= max_total:
            raise ValueError(f"total_target must be between 1 and {max_total}")

        resolved_mode = validate_send_mode(send_mode, self.settings.send_mode)
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        niche_key = niche or self.settings.default_niche
        preset = get_niche_preset(niche_key)

        merged_raw = await self._discover_raw(preset, total_target)
        all_profiles: list[InfluencerProfile] = []
        if not merged_raw:
            self.db.upsert_influencers([], run_id=run_id, mark_previous_inactive=True)
        else:
            for batch_idx in range(0, len(merged_raw), batch_size):
                raw_chunk = merged_raw[batch_idx : batch_idx + batch_size]
                chunk_profiles = await self._evaluate_and_enrich(raw_chunk, preset, run_id)
                self.db.upsert_influencers(
                    chunk_profiles,
                    run_id=run_id,
                    mark_previous_inactive=(batch_idx == 0),
                )
                all_profiles.extend(chunk_profiles)

        messages = await self.run_personalization(
            force=force_regenerate,
            run_id=run_id,
            batch_size=batch_size,
        )
        dispatch_res = self.run_sending(
            mode=resolved_mode, simulate_dms=simulate_dms, run_id=run_id
        )
        summary = self._build_summary(
            run_id=run_id,
            niche_key=niche_key,
            profiles=all_profiles,
            messages=messages,
            dispatch_res=dispatch_res,
        )
        self.db.save_pipeline_run(summary)
        self.export_all_artifacts()
        return summary

    def export_all_artifacts(self) -> dict[str, Path]:
        """Write UTF-8 BOM CSV, JSON, and Markdown submission deliverables under outputs/."""
        out = self.outputs_dir
        out.mkdir(parents=True, exist_ok=True)

        current_run_id = self.db.get_current_run_id()
        all_profiles = self.db.get_influencers(current_only=True)
        qualified = [p for p in all_profiles if p.status == FilterStatus.QUALIFIED]
        messages = self.db.get_messages(current_only=True)
        outreach_logs = (
            self.db.get_outreach_log(run_id=current_run_id)
            if current_run_id
            else self.db.get_outreach_log()
        )
        all_history_logs = self.db.get_outreach_log()
        dm_queue = self.db.get_dm_queue(current_only=True)

        # 1. Influencer Dataset (CSV with UTF-8 BOM + JSON)
        dataset_csv = out / "influencers_dataset.csv"
        with dataset_csv.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "ID",
                    "Run ID",
                    "Name",
                    "Platform",
                    "Followers",
                    "Engagement",
                    "Niche",
                    "Email",
                    "Email Source",
                    "Outreach Ready",
                    "Profile URL",
                    "Content Theme",
                    "Status",
                    "Tier",
                    "Filter Score",
                    "Brand Fit Score",
                    "Niche Relevance",
                    "Instagram URL",
                    "YouTube URL",
                    "TikTok URL",
                    "Website",
                    "Audience Geography",
                    "Audience Age",
                    "Audience Gender",
                    "Content Tone Style",
                    "Recent Content Sample",
                    "Cross-Platform Note",
                    "Passed Criteria",
                    "Failed Criteria",
                    "Filter Summary",
                    "Engagement Formula",
                ]
            )
            for p in all_profiles:
                writer.writerow(
                    [
                        p.id,
                        p.run_id,
                        p.name,
                        p.platform,
                        p.followers if p.followers is not None else "Unavailable",
                        f"{p.engagement_rate:.2f}%" if p.engagement_rate is not None else "Unavailable",
                        p.niche,
                        p.contact_email,
                        p.email_source,
                        p.outreach_readiness,
                        p.profile_url,
                        ", ".join(p.content_themes),
                        p.status.value,
                        p.tier,
                        p.filter_score,
                        p.brand_fit_score,
                        p.niche_relevance_score,
                        p.instagram_url,
                        p.youtube_url,
                        p.tiktok_url,
                        p.website,
                        p.audience_geography,
                        p.audience_age,
                        p.audience_gender,
                        p.content_tone_style,
                        p.recent_content[0].title if p.recent_content else "None",
                        p.cross_platform_note or "None",
                        " | ".join(p.passed_criteria),
                        " | ".join(p.failed_criteria),
                        p.filter_summary,
                        p.engagement_formula,
                    ]
                )

        dataset_json = out / "influencers_dataset.json"
        dataset_json.write_text(
            json.dumps([p.model_dump() for p in all_profiles], indent=2),
            encoding="utf-8",
        )

        # 2. Shortlisted Enriched JSON
        shortlisted_json = out / "shortlisted_enriched.json"
        shortlisted_json.write_text(
            json.dumps([p.model_dump() for p in qualified], indent=2),
            encoding="utf-8",
        )

        # 3. Personalized Messages (CSV + JSON + Markdown)
        msg_csv = out / "personalized_messages.csv"
        with msg_csv.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "Run ID",
                    "Influencer ID",
                    "Influencer",
                    "Platform",
                    "Email",
                    "Instagram URL",
                    "Collaboration Angle",
                    "Email Subject",
                    "Email Pitch (60-90w)",
                    "Email Word Count",
                    "Instagram DM (15-30w)",
                    "DM Word Count",
                    "Personalization Signals",
                    "Generation Mode",
                    "LLM Provider",
                    "LLM Model",
                    "Generated At",
                ]
            )
            for m in messages:
                writer.writerow(
                    [
                        m.run_id,
                        m.influencer_id,
                        m.influencer_name,
                        m.platform,
                        m.contact_email,
                        m.instagram_url,
                        m.collaboration_angle,
                        m.email_subject,
                        m.email_pitch,
                        m.email_word_count,
                        m.instagram_dm,
                        m.dm_word_count,
                        " | ".join(m.personalization_signals_used),
                        m.generation_mode,
                        m.llm_provider,
                        m.llm_model,
                        m.generated_at,
                    ]
                )

        msg_json = out / "personalized_messages.json"
        msg_json.write_text(
            json.dumps([m.model_dump() for m in messages], indent=2),
            encoding="utf-8",
        )

        msg_md = out / "personalized_messages.md"
        md_lines = [
            "# Personalized Outreach Messages (Shortlisted Micro-Influencers)",
            "",
            f"Total shortlisted messages (current run): **{len(messages)}**",
            "",
        ]
        for idx, m in enumerate(messages, start=1):
            md_lines.extend(
                [
                    f"## {idx}. {m.influencer_name} ({m.platform})",
                    f"- **Run ID:** `{m.run_id or 'N/A'}` | **Email:** `{m.contact_email}` | **Instagram:** `{m.instagram_url}`",
                    f"- **Collaboration Angle:** {m.collaboration_angle} | **Generation Mode:** `{m.generation_mode}` | **Generator:** `{m.llm_provider} / {m.llm_model}`",
                    f"- **Email Subject:** {m.email_subject}",
                    f"- **Email Pitch ({m.email_word_count} words):**",
                    f"> {m.email_pitch}",
                    f"- **Instagram DM ({m.dm_word_count} words):**",
                    f"> {m.instagram_dm}",
                    "",
                ]
            )
        msg_md.write_text("\n".join(md_lines), encoding="utf-8")

        # 4. Outreach Tracker (CSV with UTF-8 BOM + JSON)
        tracker_csv = out / "outreach_tracker.csv"
        with tracker_csv.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "Run ID",
                    "Influencer ID",
                    "Influencer",
                    "Email",
                    "Message Generated",
                    "Sent",
                    "Date",
                    "Status",
                    "Email Subject",
                    "Send Mode",
                    "Receipt ID",
                    "Notes",
                ]
            )
            for row in outreach_logs:
                writer.writerow(
                    [
                        row.run_id,
                        row.influencer_id,
                        row.influencer_name,
                        row.email,
                        "Yes" if row.message_generated else "No",
                        "Yes" if row.sent else "No",
                        row.sent_date or "-",
                        row.status.value,
                        row.email_subject,
                        row.send_mode,
                        row.receipt_id or "-",
                        row.notes,
                    ]
                )

        tracker_json = out / "outreach_tracker.json"
        tracker_json.write_text(
            json.dumps([r.model_dump() for r in outreach_logs], indent=2),
            encoding="utf-8",
        )

        history_json = out / "outreach_history.json"
        history_json.write_text(
            json.dumps([r.model_dump() for r in all_history_logs], indent=2),
            encoding="utf-8",
        )

        # 5. Instagram DM Queue CSV (UTF-8 BOM)
        dm_csv = out / "instagram_dm_queue.csv"
        with dm_csv.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "Run ID",
                    "Influencer",
                    "Instagram URL",
                    "Instagram DM",
                    "Word Count",
                    "Sent",
                    "Date",
                    "Status",
                    "Last Event Status",
                    "Duplicate Attempts",
                    "Notes",
                ]
            )
            for dm in dm_queue:
                writer.writerow(
                    [
                        dm.run_id,
                        dm.influencer_name,
                        dm.instagram_url,
                        dm.instagram_dm,
                        dm.dm_word_count,
                        "Yes" if dm.sent else "No",
                        dm.sent_date or "-",
                        dm.status.value,
                        dm.last_event_status.value,
                        dm.duplicate_attempts,
                        dm.notes,
                    ]
                )

        return {
            "influencers_dataset_csv": dataset_csv,
            "influencers_dataset_json": dataset_json,
            "shortlisted_enriched_json": shortlisted_json,
            "personalized_messages_csv": msg_csv,
            "personalized_messages_json": msg_json,
            "personalized_messages_md": msg_md,
            "outreach_tracker_csv": tracker_csv,
            "outreach_tracker_json": tracker_json,
            "outreach_history_json": history_json,
            "instagram_dm_queue_csv": dm_csv,
        }
