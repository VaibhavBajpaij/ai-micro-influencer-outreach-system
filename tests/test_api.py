"""Integration tests for FastAPI security, input validation, bulk safety, and endpoints."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outreach_system.api import create_app
from outreach_system.config import Settings
from outreach_system.models import FilterStatus, InfluencerProfile, PersonalizedMessage
from outreach_system.sending import InstagramDMWorkflow
from outreach_system.storage import OutreachDatabase

API_KEY = "unit-test-secret-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}


def _seed_api_db(db_path: Path, run_id: str = "run-api1") -> None:
    db = OutreachDatabase(db_path)
    prof = InfluencerProfile(
        run_id=run_id,
        id="yt_api1",
        name="API Tester",
        platform="YouTube",
        profile_url="https://www.youtube.com/@apitester",
        followers=19_000,
        engagement_rate=1.5,
        niche="Technology & AI Gadgets",
        content_themes=["AI & Automation Tools"],
        contact_email="collab@apitester.io",
        outreach_ready=True,
        outreach_readiness="Outreach-Ready (Email Verified)",
        instagram_url="https://instagram.com/apitester",
        status=FilterStatus.QUALIFIED,
        tier="Tier A (Priority Micro)",
        filter_score=86.0,
    )
    db.upsert_influencers([prof], run_id=run_id, mark_previous_inactive=True)
    msg = PersonalizedMessage(
        run_id=run_id,
        influencer_id="yt_api1",
        influencer_name="API Tester",
        platform="YouTube",
        contact_email="collab@apitester.io",
        instagram_url="https://instagram.com/apitester",
        collaboration_angle="Sponsorship",
        email_subject="VoltFlow x API Tester: Creator Sponsorship",
        email_pitch=" ".join(["pitch"] * 70),
        email_word_count=70,
        instagram_dm=" ".join(["dm"] * 18),
        dm_word_count=18,
        personalization_signals_used=["niche:AI"],
        llm_provider="contextual_fallback",
        llm_model="dynamic-nlg-v1",
        generated_at=datetime.now(UTC).isoformat(),
    )
    db.save_run_messages([msg], run_id=run_id)
    InstagramDMWorkflow(db).sync_queue([msg], run_id=run_id)


def test_fastapi_endpoints_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "api.db"
    out_dir = tmp_path / "outputs"
    settings = Settings(
        database_path=str(db_path),
        outputs_dir=str(out_dir),
        llm_provider="fallback",
        api_auth_token=API_KEY,
    )
    _seed_api_db(db_path)
    client = TestClient(create_app(settings))

    home = client.get("/")
    assert home.status_code == 200
    assert API_KEY not in home.text  # Secret must never appear in frontend HTML

    assert client.get("/api/health").json()["status"] == "ok"
    summary = client.get("/api/summary", headers=AUTH_HEADERS).json()
    assert summary["discovered_count"] == 1
    assert summary["qualified_count"] == 1
    assert summary["outreach_ready_count"] == 1
    assert summary["messages_generated_count"] == 1
    assert summary["messages_llm_count"] == 0
    assert summary["messages_fallback_count"] == 1
    assert summary["send_mode"] == "simulate"
    assert summary["allow_live_smtp"] is False

    send_res = client.post(
        "/api/outreach/send",
        headers=AUTH_HEADERS,
        json={"mode": "simulate", "influencer_ids": ["yt_api1"]},
    )
    assert send_res.status_code == 200
    assert send_res.json()["sent"] == 1

    dm_res = client.post("/api/dm/yt_api1/simulate", headers=AUTH_HEADERS)
    assert dm_res.status_code == 200
    assert dm_res.json()["status"] == "Simulated Sent"


def test_summary_reports_accurate_dm_sent_and_skipped_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "dm_summary.db"
    settings = Settings(
        database_path=str(db_path),
        outputs_dir=str(tmp_path / "out"),
        llm_provider="fallback",
        api_auth_token=API_KEY,
    )
    db = OutreachDatabase(db_path)
    p_with_ig = InfluencerProfile(
        run_id="run-s1",
        id="yt_ig_ok",
        name="IG Valid",
        platform="YouTube",
        profile_url="https://www.youtube.com/@igvalid",
        followers=20_000,
        engagement_rate=1.2,
        niche="Technology & AI Gadgets",
        contact_email="Not Found",
        instagram_url="https://instagram.com/igvalid",
        status=FilterStatus.QUALIFIED,
        tier="Tier A (Priority Micro)",
        filter_score=85.0,
    )
    p_no_ig = InfluencerProfile(
        run_id="run-s1",
        id="yt_ig_missing",
        name="IG Missing",
        platform="YouTube",
        profile_url="https://www.youtube.com/@igmissing",
        followers=22_000,
        engagement_rate=1.3,
        niche="Technology & AI Gadgets",
        contact_email="Not Found",
        instagram_url="Not Found",
        status=FilterStatus.QUALIFIED,
        tier="Tier A (Priority Micro)",
        filter_score=84.0,
    )
    db.upsert_influencers([p_with_ig, p_no_ig], run_id="run-s1", mark_previous_inactive=True)
    msgs = [
        PersonalizedMessage(
            run_id="run-s1",
            influencer_id=p.id,
            influencer_name=p.name,
            platform=p.platform,
            contact_email=p.contact_email,
            instagram_url=p.instagram_url,
            collaboration_angle="Sponsorship",
            email_subject=f"VoltFlow x {p.name}",
            email_pitch=" ".join(["pitch"] * 70),
            email_word_count=70,
            instagram_dm=" ".join(["dm"] * 18),
            dm_word_count=18,
            personalization_signals_used=["niche:AI"],
            llm_provider="contextual_fallback",
            llm_model="dynamic-nlg-v1",
            generated_at=datetime.now(UTC).isoformat(),
        )
        for p in (p_with_ig, p_no_ig)
    ]
    db.save_run_messages(msgs, run_id="run-s1")
    InstagramDMWorkflow(db).sync_queue(msgs, run_id="run-s1")

    client = TestClient(create_app(settings))
    r1 = client.post("/api/dm/yt_ig_ok/simulate", headers=AUTH_HEADERS)
    assert r1.status_code == 200
    assert r1.json()["status"] == "Simulated Sent"
    assert r1.json()["sent"] is True

    r2 = client.post("/api/dm/yt_ig_missing/simulate", headers=AUTH_HEADERS)
    assert r2.status_code == 200
    assert r2.json()["status"] == "Skipped - No Instagram Handle"
    assert r2.json()["sent"] is False

    summary = client.get("/api/summary", headers=AUTH_HEADERS).json()
    assert summary["dm_queued_count"] == 2
    assert summary["dm_sent_count"] == 1
    assert summary["dm_skipped_no_handle"] == 1
    assert summary["dm_pending_count"] == 0


def test_read_endpoints_require_auth_while_health_and_shell_remain_public(tmp_path: Path) -> None:
    db_path = tmp_path / "read_auth.db"
    _seed_api_db(db_path)

    # 1. Server without configured token fails closed with 503 on sensitive reads
    unconfigured = TestClient(
        create_app(Settings(database_path=str(db_path), outputs_dir=str(tmp_path / "out1"), api_auth_token=""))
    )
    assert unconfigured.get("/").status_code == 200
    assert unconfigured.get("/api/health").status_code == 200
    for path in (
        "/api/summary",
        "/api/influencers",
        "/api/messages",
        "/api/messages/history",
        "/api/outreach-log",
        "/api/dm-queue",
    ):
        assert unconfigured.get(path).status_code == 503

    # 2. Server with configured token rejects missing or wrong tokens with 401 on reads
    client = TestClient(
        create_app(
            Settings(database_path=str(db_path), outputs_dir=str(tmp_path / "out2"), api_auth_token=API_KEY)
        )
    )
    assert client.get("/").status_code == 200
    assert "collab@apitester.io" not in client.get("/").text
    assert client.get("/api/health").status_code == 200

    for path in (
        "/api/summary",
        "/api/influencers",
        "/api/messages",
        "/api/messages/history",
        "/api/outreach-log",
        "/api/dm-queue",
    ):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get(path, headers=AUTH_HEADERS).status_code == 200
        assert client.get(path, headers={"Authorization": f"Bearer {API_KEY}"}).status_code == 200


def test_unknown_and_ineligible_influencer_ids_return_404_or_400(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown_ids.db"
    _seed_api_db(db_path)
    db = OutreachDatabase(db_path)
    db.upsert_influencers(
        [
            InfluencerProfile(
                run_id="run-api1",
                id="yt_rejected",
                name="Rejected Macro",
                platform="YouTube",
                profile_url="https://www.youtube.com/@rejectedmacro",
                followers=500_000,
                engagement_rate=1.2,
                niche="Technology & AI Gadgets",
                contact_email="macro@example.org",
                status=FilterStatus.REJECTED,
                tier="Rejected (Macro / Mega)",
            )
        ],
        run_id="run-api1",
        mark_previous_inactive=False,
    )

    client = TestClient(
        create_app(
            Settings(database_path=str(db_path), outputs_dir=str(tmp_path / "out"), api_auth_token=API_KEY)
        )
    )

    resp_unknown = client.post(
        "/api/outreach/send",
        headers=AUTH_HEADERS,
        json={"mode": "simulate", "influencer_ids": ["yt_does_not_exist"]},
    )
    assert resp_unknown.status_code == 404

    resp_rejected = client.post(
        "/api/outreach/send",
        headers=AUTH_HEADERS,
        json={"mode": "simulate", "influencer_ids": ["yt_rejected"]},
    )
    assert resp_rejected.status_code == 400


def test_mutating_endpoints_require_auth_and_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.db"
    _seed_api_db(db_path)

    # 1. Server without configured token fails closed with 503
    unconfigured_client = TestClient(
        create_app(Settings(database_path=str(db_path), outputs_dir=str(tmp_path / "out1"), api_auth_token=""))
    )
    assert (
        unconfigured_client.post("/api/pipeline/run", json={"niche": "technology"}).status_code
        == 503
    )

    # 2. Server with configured token rejects missing or wrong tokens with 401
    client = TestClient(
        create_app(
            Settings(database_path=str(db_path), outputs_dir=str(tmp_path / "out2"), api_auth_token=API_KEY)
        )
    )
    protected_calls = [
        ("POST", "/api/pipeline/run", {"niche": "technology", "target_count": 50}),
        ("POST", "/api/personalize/regenerate", None),
        ("POST", "/api/outreach/send", {"mode": "simulate", "influencer_ids": ["yt_api1"]}),
        ("POST", "/api/outreach/send-bulk", {"mode": "simulate", "confirm_bulk": True}),
        ("POST", "/api/dm/yt_api1/simulate", None),
        ("POST", "/api/dm/yt_api1/mark-sent", None),
    ]
    for method, path, body in protected_calls:
        assert client.request(method, path, json=body).status_code == 401
        assert (
            client.request(method, path, json=body, headers={"X-API-Key": "wrong"}).status_code
            == 401
        )


def test_request_validation_target_count_send_mode_and_empty_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "val.db"
    _seed_api_db(db_path)
    client = TestClient(
        create_app(
            Settings(
                database_path=str(db_path),
                outputs_dir=str(tmp_path / "out"),
                api_auth_token=API_KEY,
                allow_live_smtp=False,
            )
        )
    )

    # target_count bounds (1..200)
    for bad_count in (0, -5, 201, "invalid"):
        resp = client.post(
            "/api/pipeline/run",
            headers=AUTH_HEADERS,
            json={"niche": "technology", "target_count": bad_count},
        )
        assert resp.status_code == 422

    # Unsupported send mode ('live') must fail 422, not fall through to simulate
    resp_live = client.post(
        "/api/outreach/send",
        headers=AUTH_HEADERS,
        json={"mode": "live", "influencer_ids": ["yt_api1"]},
    )
    assert resp_live.status_code == 422

    # mode='smtp' requires explicit allow_live_smtp=True -> 403 when disabled
    resp_smtp = client.post(
        "/api/outreach/send",
        headers=AUTH_HEADERS,
        json={"mode": "smtp", "influencer_ids": ["yt_api1"]},
    )
    assert resp_smtp.status_code == 403

    # Empty influencer_ids must return HTTP 400 (never silently dispatch all)
    resp_empty = client.post(
        "/api/outreach/send",
        headers=AUTH_HEADERS,
        json={"mode": "simulate", "influencer_ids": []},
    )
    assert resp_empty.status_code == 400

    # Bulk endpoint requires explicit confirm_bulk=True
    resp_bulk_unconfirmed = client.post(
        "/api/outreach/send-bulk",
        headers=AUTH_HEADERS,
        json={"mode": "simulate", "confirm_bulk": False},
    )
    assert resp_bulk_unconfirmed.status_code == 400

    resp_bulk_ok = client.post(
        "/api/outreach/send-bulk",
        headers=AUTH_HEADERS,
        json={"mode": "simulate", "confirm_bulk": True},
    )
    assert resp_bulk_ok.status_code == 200
    assert resp_bulk_ok.json()["sent"] == 1


def test_verify_smtp_endpoint_run_batch_and_shared_db_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from outreach_system.models import EmailSendStatus

    db_path = tmp_path / "verify_api.db"
    _seed_api_db(db_path)
    settings = Settings(
        database_path=str(db_path),
        outputs_dir=str(tmp_path / "outputs"),
        api_auth_token=API_KEY,
        allow_live_smtp=False,
    )

    init_calls = {"n": 0}
    orig_init_schema = OutreachDatabase._init_schema

    def counted_init_schema(self):
        init_calls["n"] += 1
        return orig_init_schema(self)

    monkeypatch.setattr(OutreachDatabase, "_init_schema", counted_init_schema)

    client = TestClient(create_app(settings))
    # Multiple read requests reuse the shared DB handle and do not trigger schema DDL
    for _ in range(5):
        assert client.get("/api/summary", headers=AUTH_HEADERS).status_code == 200
        assert client.get("/api/influencers", headers=AUTH_HEADERS).status_code == 200
    assert init_calls["n"] <= 1

    # Seed an uncertain SMTP row directly and test /api/outreach/{log_id}/verify-smtp
    db = OutreachDatabase(db_path)
    _, log_id, _ = db.claim_outreach_slot(
        run_id="run-api1",
        influencer_id="yt_api1",
        influencer_name="API Tester",
        email="collab@apitester.io",
        email_subject="Subject",
        send_mode="smtp",
        receipt_id="smtp-uncertain1",
    )
    db.finalize_outreach_slot(
        log_id,
        status=EmailSendStatus.MANUAL_VERIFICATION_REQUIRED,
        sent=False,
        sent_date=None,
        receipt_id="smtp-uncertain1",
        notes="Connection lost after DATA",
    )

    assert client.post(f"/api/outreach/{log_id}/verify-smtp", json={"delivered": True}).status_code == 401
    assert (
        client.post(
            "/api/outreach/99999/verify-smtp",
            headers=AUTH_HEADERS,
            json={"delivered": True},
        ).status_code
        == 404
    )

    v_res = client.post(
        f"/api/outreach/{log_id}/verify-smtp",
        headers=AUTH_HEADERS,
        json={"delivered": False, "notes": "Verified bounce log"},
    )
    assert v_res.status_code == 200
    assert v_res.json()["status"] == EmailSendStatus.FAILED.value

    # Trying to verify an already-resolved row returns 400
    v_again = client.post(
        f"/api/outreach/{log_id}/verify-smtp",
        headers=AUTH_HEADERS,
        json={"delivered": True},
    )
    assert v_again.status_code == 400

    # /api/pipeline/run-batch enforces batch_size <= 200 and auth
    assert (
        client.post(
            "/api/pipeline/run-batch",
            headers=AUTH_HEADERS,
            json={"niche": "technology", "total_target": 250, "batch_size": 201},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/pipeline/run-batch",
            json={"niche": "technology", "total_target": 250, "batch_size": 100},
        ).status_code
        == 401
    )
