"""Automated regression tests for legacy SQLite schema migration."""

import json
import sqlite3
from pathlib import Path

from outreach_system.models import FilterStatus, InfluencerProfile
from outreach_system.storage import OutreachDatabase


def test_legacy_database_migration_preserves_data_and_adds_constraints(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    prof = InfluencerProfile(
        id="yt_legacy1",
        name="Legacy Creator",
        platform="YouTube",
        profile_url="https://www.youtube.com/@legacy1",
        followers=25_000,
        engagement_rate=1.2,
        niche="Technology & AI Gadgets",
        contact_email="legacy@creator.io",
        status=FilterStatus.QUALIFIED,
        tier="Tier A (Priority Micro)",
    )

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE influencers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            handle TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL,
            profile_url TEXT NOT NULL,
            followers INTEGER,
            engagement_rate REAL,
            niche TEXT NOT NULL,
            contact_email TEXT NOT NULL DEFAULT 'Not Found',
            status TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT '',
            filter_score REAL NOT NULL DEFAULT 0.0,
            audience_geography TEXT NOT NULL DEFAULT 'Not Available',
            profile_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE personalized_messages (
            influencer_id TEXT PRIMARY KEY,
            influencer_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            contact_email TEXT NOT NULL,
            instagram_url TEXT NOT NULL DEFAULT 'Not Found',
            collaboration_angle TEXT NOT NULL,
            email_subject TEXT NOT NULL,
            email_pitch TEXT NOT NULL,
            email_word_count INTEGER NOT NULL,
            instagram_dm TEXT NOT NULL,
            dm_word_count INTEGER NOT NULL,
            signals_json TEXT NOT NULL,
            llm_provider TEXT NOT NULL,
            llm_model TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );

        CREATE TABLE outreach_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            influencer_id TEXT NOT NULL,
            influencer_name TEXT NOT NULL,
            email TEXT NOT NULL,
            email_normalized TEXT NOT NULL,
            message_generated INTEGER NOT NULL DEFAULT 0,
            email_subject TEXT NOT NULL DEFAULT '',
            sent INTEGER NOT NULL DEFAULT 0,
            sent_date TEXT,
            status TEXT NOT NULL,
            send_mode TEXT NOT NULL DEFAULT 'simulate',
            receipt_id TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE dm_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            influencer_id TEXT NOT NULL UNIQUE,
            influencer_name TEXT NOT NULL,
            instagram_url TEXT NOT NULL,
            instagram_dm TEXT NOT NULL,
            dm_word_count INTEGER NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            sent_date TEXT,
            status TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO influencers (
            id, name, handle, platform, profile_url, followers, engagement_rate,
            niche, contact_email, status, tier, filter_score, audience_geography, profile_json, updated_at
        ) VALUES (?, ?, '', 'YouTube', ?, 25000, 1.2, 'Technology & AI Gadgets', 'legacy@creator.io', 'Qualified', 'Tier A', 80.0, 'US', ?, '2026-01-01T00:00:00Z')
        """,
        (prof.id, prof.name, prof.profile_url, prof.model_dump_json()),
    )
    conn.execute(
        """
        INSERT INTO personalized_messages (
            influencer_id, influencer_name, platform, contact_email, instagram_url,
            collaboration_angle, email_subject, email_pitch, email_word_count,
            instagram_dm, dm_word_count, signals_json, llm_provider, llm_model, generated_at
        ) VALUES (?, ?, 'YouTube', 'legacy@creator.io', 'Not Found', 'Sponsorship', 'Subject', 'pitch words', 2, 'dm words', 2, '["niche:tech"]', 'contextual_fallback', 'dynamic-nlg-v1', '2026-01-01T00:00:00Z')
        """,
        (prof.id, prof.name),
    )
    conn.execute(
        """
        INSERT INTO outreach_log (
            influencer_id, influencer_name, email, email_normalized, message_generated,
            email_subject, sent, sent_date, status, send_mode, receipt_id, notes, created_at
        ) VALUES (?, ?, 'legacy@creator.io', 'legacy@creator.io', 1, 'Subject', 1, '2026-01-01T00:00:00Z', 'Sent (Simulated)', 'simulate', 'sim-old', 'legacy row', '2026-01-01T00:00:00Z')
        """,
        (prof.id, prof.name),
    )
    conn.execute(
        """
        INSERT INTO dm_queue (
            influencer_id, influencer_name, instagram_url, instagram_dm, dm_word_count,
            sent, sent_date, status, notes, updated_at
        ) VALUES (?, ?, 'https://instagram.com/legacy1', 'dm words', 2, 1, '2026-01-01T00:00:00Z', 'Simulated Sent', 'legacy dm', '2026-01-01T00:00:00Z')
        """,
        (prof.id, prof.name),
    )
    conn.commit()
    conn.close()

    # Open through OutreachDatabase -> triggers automatic in-place schema migration
    migrated_db = OutreachDatabase(db_path)

    # Verify all legacy rows survived intact
    assert [p.id for p in migrated_db.get_influencers()] == ["yt_legacy1"]
    msgs = migrated_db.get_messages(current_only=True)
    assert len(msgs) == 1
    assert msgs[0].run_id == "legacy"
    assert msgs[0].influencer_id == "yt_legacy1"

    logs = migrated_db.get_outreach_log()
    assert len(logs) == 1
    assert logs[0].status.value == "Sent (Simulated)"

    dms = migrated_db.get_dm_queue(current_only=True)
    assert len(dms) == 1
    assert dms[0].sent is True
    assert dms[0].status.value == "Simulated Sent"

    # Verify atomic unique constraints exist on migrated outreach_log
    assert migrated_db.has_sent_outreach("yt_legacy1", "legacy@creator.io") is True
    claimed, _, dup_entry = migrated_db.claim_outreach_slot(
        run_id="run-new",
        influencer_id="yt_legacy1",
        influencer_name="Legacy Creator",
        email="legacy@creator.io",
        email_subject="Subject",
        send_mode="simulate",
        receipt_id="sim-new",
    )
    assert claimed is False
    assert dup_entry is not None
    assert dup_entry.status.value == "Skipped - Duplicate"

    # Verify new columns exist cleanly
    check_conn = sqlite3.connect(db_path)
    inf_cols = {r[1] for r in check_conn.execute("PRAGMA table_info(influencers)").fetchall()}
    dm_cols = {r[1] for r in check_conn.execute("PRAGMA table_info(dm_queue)").fetchall()}
    check_conn.close()
    assert {"run_id", "is_current"}.issubset(inf_cols)
    assert {"run_id", "is_current", "last_event_status", "duplicate_attempts"}.issubset(dm_cols)
    assert json.loads(prof.model_dump_json())["id"] == "yt_legacy1"

