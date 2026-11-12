"""Tests verifying multi-run isolation and zero stale-message accumulation."""

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outreach_system.api import create_app
from outreach_system.config import Settings
from outreach_system.models import ContentItem, DMStatus, FilterStatus, InfluencerRaw
from outreach_system.pipeline import OutreachPipeline


def _raw_creator(
    creator_id: str,
    name: str,
    email: str | None,
    ig_handle: str | None = None,
) -> InfluencerRaw:
    recent = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    bio = "Tech hardware reviews, AI developer productivity tools, and minimalist desk setups."
    if email:
        bio = f"{bio} Business: {email}"
    links = [f"https://instagram.com/{ig_handle}"] if ig_handle else []
    return InfluencerRaw(
        id=creator_id,
        name=name,
        handle=f"@{creator_id}",
        platform="YouTube",
        profile_url=f"https://www.youtube.com/@{creator_id}",
        followers=25_000,
        avg_views=5_500.0,
        avg_likes=280.0,
        engagement_rate=1.12,
        bio=bio,
        external_links=links,
        country="United States",
        recent_content=[
            ContentItem(
                title=f"Hands-on AI Desk Workflow Review by {name}",
                url=f"https://youtube.com/watch?v={creator_id}",
                published_at=recent,
                views=5500,
                likes=280,
            )
        ],
        last_post_date=recent,
    )


@pytest.mark.asyncio
async def test_multiple_pipeline_runs_prevent_stale_message_accumulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_path=str(tmp_path / "runs.db"),
        outputs_dir=str(tmp_path / "outputs"),
        llm_provider="fallback",
    )
    pipeline = OutreachPipeline(settings)

    batches = {
        "yt": [
            [
                _raw_creator("yt_a", "Creator Alpha", "alpha@tech.dev"),
                _raw_creator("yt_b", "Creator Beta", "beta@tech.dev"),
            ],
            [
                _raw_creator("yt_c", "Creator Gamma", "gamma@tech.dev"),
            ],
        ]
    }
    call_idx = {"i": 0}

    async def fake_yt_discover(self, preset, limit=40):
        idx = call_idx["i"]
        call_idx["i"] += 1
        return batches["yt"][idx]

    async def fake_bsky_discover(self, preset, limit=15):
        return []

    monkeypatch.setattr("outreach_system.discovery.youtube.YouTubeDiscoverer.discover", fake_yt_discover)
    monkeypatch.setattr("outreach_system.discovery.bluesky.BlueskyDiscoverer.discover", fake_bsky_discover)

    # Run 1 -> qualifies Alpha and Beta
    summary1 = await pipeline.run_full_pipeline(niche="technology", target_count=10, send_mode="simulate")
    assert summary1.qualified_count == 2
    assert summary1.messages_generated_count == 2
    assert {m.influencer_id for m in pipeline.db.get_messages(current_only=True)} == {"yt_a", "yt_b"}

    # Run 2 -> qualifies ONLY Gamma; Alpha and Beta must NOT contaminate current messages or DM queue
    summary2 = await pipeline.run_full_pipeline(niche="technology", target_count=10, send_mode="simulate")
    assert summary2.discovered_count == 1
    assert summary2.qualified_count == 1
    assert summary2.messages_generated_count == 1
    assert summary2.dm_queued_count == 1

    current_msgs = pipeline.db.get_messages(current_only=True)
    current_dms = pipeline.db.get_dm_queue(current_only=True)
    current_qualified = pipeline.db.get_influencers(status=FilterStatus.QUALIFIED, current_only=True)
    assert [m.influencer_id for m in current_msgs] == ["yt_c"]
    assert [d.influencer_id for d in current_dms] == ["yt_c"]
    assert [q.id for q in current_qualified] == ["yt_c"]

    # Historical messages from Run 1 remain preserved for auditing
    all_history = pipeline.db.get_messages(current_only=False)
    assert {m.influencer_id for m in all_history} == {"yt_a", "yt_b", "yt_c"}


@pytest.mark.asyncio
async def test_multi_run_sent_and_skipped_kpis_and_dm_history_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_key = "multi-run-key"
    settings = Settings(
        database_path=str(tmp_path / "multi.db"),
        outputs_dir=str(tmp_path / "outputs"),
        llm_provider="fallback",
        api_auth_token=api_key,
    )
    pipeline = OutreachPipeline(settings)

    batches = [
        [
            _raw_creator("yt_alpha", "Creator Alpha", "alpha@tech.dev", ig_handle="alpha_tech"),
            _raw_creator("yt_beta", "Creator Beta", None, ig_handle=None),
        ],
        [
            _raw_creator("yt_alpha", "Creator Alpha", "alpha@tech.dev", ig_handle="alpha_tech"),
            _raw_creator("yt_gamma", "Creator Gamma", "gamma@tech.dev", ig_handle="gamma_tech"),
            _raw_creator("yt_delta", "Creator Delta", None, ig_handle=None),
        ],
    ]
    call_idx = {"i": 0}

    async def fake_yt_discover(self, preset, limit=40):
        idx = call_idx["i"]
        call_idx["i"] += 1
        return batches[idx]

    async def fake_bsky_discover(self, preset, limit=15):
        return []

    monkeypatch.setattr("outreach_system.discovery.youtube.YouTubeDiscoverer.discover", fake_yt_discover)
    monkeypatch.setattr("outreach_system.discovery.bluesky.BlueskyDiscoverer.discover", fake_bsky_discover)

    # Run 1: Alpha sent (simulated), Beta skipped (no email), DMs simulated
    s1 = await pipeline.run_full_pipeline(niche="technology", target_count=10, send_mode="simulate", simulate_dms=True)
    assert s1.emails_sent_count == 1
    assert s1.emails_skipped_no_email == 1
    assert s1.emails_skipped_duplicate == 0

    alpha_dm_run1 = next(d for d in pipeline.db.get_dm_queue(current_only=True) if d.influencer_id == "yt_alpha")
    beta_dm_run1 = next(d for d in pipeline.db.get_dm_queue(current_only=True) if d.influencer_id == "yt_beta")
    assert alpha_dm_run1.run_id == s1.run_id
    assert alpha_dm_run1.sent is True
    assert alpha_dm_run1.status == DMStatus.SIMULATED_SENT
    assert beta_dm_run1.sent is False
    assert beta_dm_run1.status == DMStatus.SKIPPED_NO_HANDLE

    # Run 2: Alpha is duplicate, Gamma is newly sent, Delta is skipped (no email / no IG)
    s2 = await pipeline.run_full_pipeline(niche="technology", target_count=10, send_mode="simulate", simulate_dms=True)
    assert s2.run_id != s1.run_id
    assert s2.emails_sent_count == 1
    assert s2.emails_skipped_no_email == 1
    assert s2.emails_skipped_duplicate == 1
    assert s2.dm_sent_count == 2
    assert s2.dm_skipped_no_handle == 1

    # Verify Alpha's historical DM dispatch run_id and sent status were NOT overwritten by Run 2
    alpha_dm_after = next(d for d in pipeline.db.get_dm_queue(current_only=True) if d.influencer_id == "yt_alpha")
    assert alpha_dm_after.run_id == s1.run_id
    assert alpha_dm_after.sent is True

    # Verify API /api/summary and /api/outreach-log reflect ONLY Run 2 by default (not cumulative!)
    client = TestClient(create_app(settings))
    headers = {"X-API-Key": api_key}
    summary = client.get("/api/summary", headers=headers).json()
    assert summary["current_run_id"] == s2.run_id
    assert summary["emails_sent_count"] == 1
    assert summary["emails_skipped_no_email"] == 1
    assert summary["emails_skipped_duplicate"] == 1
    assert summary["dm_queued_count"] == 3
    assert summary["dm_sent_count"] == 2
    assert summary["dm_skipped_no_handle"] == 1
    assert summary["historical_emails_sent_count"] == 2

    current_logs = client.get("/api/outreach-log", headers=headers).json()
    assert len(current_logs) == 3
    assert {r["run_id"] for r in current_logs} == {s2.run_id}

    all_logs = client.get("/api/outreach-log?current_only=false", headers=headers).json()
    assert len(all_logs) == 5
    assert {r["run_id"] for r in all_logs} == {s1.run_id, s2.run_id}

    # Verify tracker CSV is scoped to Run 2 with Run ID column, while outreach_history.json holds all 5
    tracker_rows = list(csv.DictReader((tmp_path / "outputs" / "outreach_tracker.csv").open(encoding="utf-8-sig")))
    assert len(tracker_rows) == 3
    assert {r["Run ID"] for r in tracker_rows} == {s2.run_id}
    history_rows = json.loads((tmp_path / "outputs" / "outreach_history.json").read_text(encoding="utf-8"))
    assert len(history_rows) == 5


@pytest.mark.asyncio
async def test_batched_pipeline_processes_520_profiles_across_bounded_batches_with_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_path=str(tmp_path / "scale.db"),
        outputs_dir=str(tmp_path / "outputs"),
        llm_provider="fallback",
    )
    pipeline = OutreachPipeline(settings)

    # 520 discovered creators across bounded chunks (405 YT + 115 Bluesky), including shared email across batch boundary
    yt_items = []
    for i in range(405):
        if i == 0 or i == 250:
            email = "shared-dup@creatorhub.io"  # crosses batch_size=150 boundary (batch 1 vs batch 2)
        elif i < 12:
            email = f"creator{i}@creatorhub.io"
        else:
            email = None
        yt_items.append(_raw_creator(f"yt_s_{i:03d}", f"Scale Creator {i}", email, ig_handle=None))

    bsky_items = []
    for i in range(115):
        c = _raw_creator(f"bsky_s_{i:03d}", f"Scale Bsky {i}", None, ig_handle=None)
        c.platform = "Bluesky"
        c.profile_url = f"https://bsky.app/profile/scale{i}.bsky.social"
        bsky_items.append(c)

    async def fake_yt_discover(self, preset, limit=40):
        return yt_items[:limit]

    async def fake_bsky_discover(self, preset, limit=15):
        return bsky_items[:limit]

    monkeypatch.setattr("outreach_system.discovery.youtube.YouTubeDiscoverer.discover", fake_yt_discover)
    monkeypatch.setattr("outreach_system.discovery.bluesky.BlueskyDiscoverer.discover", fake_bsky_discover)

    summary = await pipeline.run_batched_pipeline(
        niche="technology",
        total_target=520,
        batch_size=150,
        send_mode="simulate",
        simulate_dms=False,
    )

    assert summary.discovered_count == 520
    assert summary.qualified_count == 520
    assert summary.generation_mode == "contextual_fallback"
    assert len(pipeline.db.get_influencers(current_only=True)) == 520

    # Shared email in batch 2 (yt_s_250) must be blocked atomically as duplicate!
    assert summary.emails_sent_count == 12  # yt_s_000 (shared) + yt_s_001..011 = 12 distinct emails
    assert summary.emails_skipped_duplicate == 1

    msg_csv_rows = list(
        csv.DictReader((tmp_path / "outputs" / "personalized_messages.csv").open(encoding="utf-8-sig"))
    )
    assert len(msg_csv_rows) == 520
    assert all(r["Generation Mode"] == "contextual_fallback" for r in msg_csv_rows)

    with pytest.raises(ValueError, match="batch_size"):
        await pipeline.run_batched_pipeline(total_target=100, batch_size=201)

