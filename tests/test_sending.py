"""Tests for email outreach sending, concurrency duplicate protection, SMTP SSL/TLS, and DM persistence."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from outreach_system.config import Settings
from outreach_system.models import (
    DMStatus,
    EmailSendStatus,
    FilterStatus,
    InfluencerProfile,
    PersonalizedMessage,
)
from outreach_system.sending import EmailOutreachSender, InstagramDMWorkflow, is_sendable_email
from outreach_system.storage import OutreachDatabase


def _seed_creator(
    db: OutreachDatabase,
    creator_id: str,
    email: str,
    ig_url: str = "Not Found",
    run_id: str = "run-1",
) -> InfluencerProfile:
    prof = InfluencerProfile(
        run_id=run_id,
        id=creator_id,
        name=f"Creator {creator_id}",
        platform="YouTube",
        profile_url=f"https://www.youtube.com/@{creator_id}",
        followers=22_000,
        engagement_rate=1.4,
        niche="Technology & AI Gadgets",
        content_themes=["AI & Automation Tools"],
        contact_email=email,
        instagram_url=ig_url,
        status=FilterStatus.QUALIFIED,
        tier="Tier A (Priority Micro)",
        filter_score=80.0,
    )
    db.upsert_influencers([prof], run_id=run_id, mark_previous_inactive=False)
    msg = PersonalizedMessage(
        run_id=run_id,
        influencer_id=creator_id,
        influencer_name=prof.name,
        platform="YouTube",
        contact_email=email,
        instagram_url=ig_url,
        collaboration_angle="Affiliate campaign",
        email_subject="VoltFlow Partnership",
        email_pitch=" ".join(["word"] * 68),
        email_word_count=68,
        instagram_dm="Hey there! Loved your recent AI workspace video and would love to share collaboration details.",
        dm_word_count=16,
        personalization_signals_used=["niche:AI"],
        llm_provider="contextual_fallback",
        llm_model="dynamic-nlg-v1",
        generated_at=datetime.now(UTC).isoformat(),
    )
    db.upsert_messages([msg])
    return prof


def test_single_char_local_part_email_is_sendable() -> None:
    assert is_sendable_email("a@gmail.com") is True
    assert is_sendable_email("x@dev.io") is True
    assert is_sendable_email("Not Found") is False
    assert is_sendable_email("") is False


def test_email_sender_skips_not_found_blocks_duplicates_and_avoids_outbox_overwrite(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    out_dir = tmp_path / "outputs"
    settings = Settings(database_path=str(db_path), outputs_dir=str(out_dir), send_mode="simulate")
    db = OutreachDatabase(db_path)

    p_valid = _seed_creator(db, "yt_valid", "hello@creatorhub.dev")
    p_missing = _seed_creator(db, "yt_missing", "Not Found")
    p_dup_email = _seed_creator(db, "yt_other_id_same_email", "HELLO@creatorhub.dev")

    sender = EmailOutreachSender(settings, db)
    first_batch = sender.dispatch_all([p_valid, p_missing])
    assert first_batch[0].status == EmailSendStatus.SENT_SIMULATED
    assert first_batch[0].sent is True
    receipt = first_batch[0].receipt_id
    eml_file = out_dir / "outbox" / f"yt_valid_{receipt}.eml"
    assert eml_file.exists()
    eml_text = eml_file.read_text(encoding="utf-8")
    assert "Reply-To:" in eml_text
    assert "List-Unsubscribe:" in eml_text
    assert first_batch[1].status == EmailSendStatus.SKIPPED_NO_EMAIL
    assert first_batch[1].sent is False

    # Re-sending same influencer OR different ID with same normalized email must block duplicates
    dup_batch = sender.dispatch_all([p_valid, p_dup_email])
    assert dup_batch[0].status == EmailSendStatus.SKIPPED_DUPLICATE
    assert dup_batch[0].sent is False
    assert dup_batch[1].status == EmailSendStatus.SKIPPED_DUPLICATE
    assert dup_batch[1].sent is False

    # Non-overwriting outbox check: a separate creator creates its own distinct file
    p_other = _seed_creator(db, "yt_second", "a@creatorhub.dev")
    second_res = sender.dispatch_single(p_other, mode="simulate")
    assert second_res.status == EmailSendStatus.SENT_SIMULATED
    assert len(list((out_dir / "outbox").glob("*.eml"))) == 2


def test_unsupported_send_mode_and_unauthorized_smtp_rejected(tmp_path: Path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "mode.db"),
        outputs_dir=str(tmp_path / "out"),
        allow_live_smtp=False,
    )
    db = OutreachDatabase(settings.resolve_database_path())
    prof = _seed_creator(db, "yt_m", "creator@example.org")
    sender = EmailOutreachSender(settings, db)

    with pytest.raises(ValueError, match="Unsupported sending mode"):
        sender.dispatch_single(prof, mode="live")

    with pytest.raises(PermissionError, match="Live SMTP sending is disabled"):
        sender.dispatch_single(prof, mode="smtp")


def test_concurrent_atomic_duplicate_protection(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    out_dir = tmp_path / "out"
    settings = Settings(database_path=str(db_path), outputs_dir=str(out_dir), send_mode="simulate")
    db = OutreachDatabase(db_path)
    prof = _seed_creator(db, "yt_race", "race@creatorhub.dev")

    def attempt_send(_: int):
        s = EmailOutreachSender(settings, OutreachDatabase(db_path))
        return s.dispatch_single(prof, mode="simulate")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt_send, range(6)))

    sent_rows = [r for r in results if r.status == EmailSendStatus.SENT_SIMULATED]
    dup_rows = [r for r in results if r.status == EmailSendStatus.SKIPPED_DUPLICATE]
    assert len(sent_rows) == 1
    assert len(dup_rows) == 5


def test_smtp_ssl_port_465_and_starttls_port_587_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_ssl = MagicMock()
    mock_smtp = MagicMock()
    monkeypatch.setattr("outreach_system.sending.email_sender.smtplib.SMTP_SSL", mock_ssl)
    monkeypatch.setattr("outreach_system.sending.email_sender.smtplib.SMTP", mock_smtp)

    # Implicit SSL on port 465
    cfg_ssl = Settings(
        database_path=str(tmp_path / "ssl.db"),
        outputs_dir=str(tmp_path / "out1"),
        allow_live_smtp=True,
        smtp_host="smtp.example.net",
        smtp_port=465,
        smtp_user="u@example.net",
        smtp_password="pw",
    )
    db1 = OutreachDatabase(cfg_ssl.resolve_database_path())
    p1 = _seed_creator(db1, "yt_ssl", "target1@creator.io")
    res1 = EmailOutreachSender(cfg_ssl, db1).dispatch_single(p1, mode="smtp")
    assert res1.status == EmailSendStatus.SENT_SMTP
    mock_ssl.assert_called_once()

    # STARTTLS on port 587
    cfg_tls = Settings(
        database_path=str(tmp_path / "tls.db"),
        outputs_dir=str(tmp_path / "out2"),
        allow_live_smtp=True,
        smtp_host="smtp.example.net",
        smtp_port=587,
        smtp_user="u@example.net",
        smtp_password="pw",
    )
    db2 = OutreachDatabase(cfg_tls.resolve_database_path())
    p2 = _seed_creator(db2, "yt_tls", "target2@creator.io")
    res2 = EmailOutreachSender(cfg_tls, db2).dispatch_single(p2, mode="smtp")
    assert res2.status == EmailSendStatus.SENT_SMTP
    mock_smtp.assert_called_once()
    mock_smtp.return_value.__enter__.return_value.starttls.assert_called_once()


def test_instagram_dm_queue_workflow_and_persists_duplicate_status(tmp_path: Path) -> None:
    db = OutreachDatabase(tmp_path / "dm.db")
    _seed_creator(db, "yt_ig", "Not Found", ig_url="https://instagram.com/creator_ig")
    wf = InstagramDMWorkflow(db)
    queue = wf.sync_queue(db.get_messages())
    assert len(queue) == 1
    assert queue[0].status == DMStatus.PENDING_MANUAL

    sent_entry = wf.simulate_send("yt_ig")
    assert sent_entry is not None
    assert sent_entry.status == DMStatus.SIMULATED_SENT
    assert sent_entry.sent is True

    dup_entry = wf.mark_sent_manually("yt_ig")
    assert dup_entry is not None
    assert dup_entry.status == DMStatus.SKIPPED_DUPLICATE
    assert dup_entry.duplicate_attempts == 1

    # Verify duplicate event persisted in SQLite dm_queue and dm_dispatch_log
    persisted = db.get_dm_queue(current_only=True)[0]
    assert persisted.sent is True
    assert persisted.status == DMStatus.SIMULATED_SENT  # original delivery preserved
    assert persisted.last_event_status == DMStatus.SKIPPED_DUPLICATE
    assert persisted.duplicate_attempts == 1

    dispatch_events = db.get_dm_dispatch_log("yt_ig")
    assert [e["status"] for e in dispatch_events] == [
        DMStatus.SKIPPED_DUPLICATE.value,
        DMStatus.SIMULATED_SENT.value,
    ]


def test_simulated_outbox_write_failure_finalizes_failed_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "fail_retry.db"
    out_dir = tmp_path / "outputs"
    settings = Settings(database_path=str(db_path), outputs_dir=str(out_dir), send_mode="simulate")
    db = OutreachDatabase(db_path)
    prof = _seed_creator(db, "yt_outbox_fail", "retry@creatorhub.dev")
    sender = EmailOutreachSender(settings, db)

    # Simulate partial write failure (creates .eml then fails writing receipt)
    def broken_write(influencer_id, mime, msg, receipt_id, timestamp):
        (sender.outbox_dir / f"{influencer_id}_{receipt_id}.eml").write_bytes(mime.as_bytes())
        raise OSError("Simulated disk full writing receipt")

    monkeypatch.setattr(sender, "_write_simulated_outbox", broken_write)
    failed_entry = sender.dispatch_single(prof, mode="simulate")
    assert failed_entry.status == EmailSendStatus.FAILED
    assert failed_entry.sent is False
    assert failed_entry.receipt_id == ""
    assert "Simulated outbox write failed" in failed_entry.notes
    assert list((out_dir / "outbox").glob("*.eml")) == []

    # Verify DB is NOT stuck in 'Sending' and retry succeeds cleanly
    monkeypatch.undo()
    retry_entry = sender.dispatch_single(prof, mode="simulate")
    assert retry_entry.status == EmailSendStatus.SENT_SIMULATED
    assert retry_entry.sent is True
    assert retry_entry.receipt_id != ""
    assert (out_dir / "outbox" / f"yt_outbox_fail_{retry_entry.receipt_id}.eml").exists()

    # Subsequent send after successful retry is blocked atomically
    dup_entry = sender.dispatch_single(prof, mode="simulate")
    assert dup_entry.status == EmailSendStatus.SKIPPED_DUPLICATE
    assert dup_entry.sent is False


def test_mime_generation_failure_finalizes_failed_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "mime_fail.db"
    out_dir = tmp_path / "outputs"
    settings = Settings(database_path=str(db_path), outputs_dir=str(out_dir), send_mode="simulate")
    db = OutreachDatabase(db_path)
    prof = _seed_creator(db, "yt_mime_fail", "mime@creatorhub.dev")
    sender = EmailOutreachSender(settings, db)

    monkeypatch.setattr(sender, "_build_mime_message", lambda *_: (_ for _ in ()).throw(ValueError("Bad MIME header")))
    failed_entry = sender.dispatch_single(prof, mode="simulate")
    assert failed_entry.status == EmailSendStatus.FAILED
    assert failed_entry.sent is False
    assert "MIME message generation failed" in failed_entry.notes

    monkeypatch.undo()
    retry_entry = sender.dispatch_single(prof, mode="simulate")
    assert retry_entry.status == EmailSendStatus.SENT_SIMULATED
    assert retry_entry.sent is True


def test_dm_skips_missing_and_not_found_handles_and_reconciles_legacy_false_sent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dm_handles.db"
    db = OutreachDatabase(db_path)

    _seed_creator(db, "yt_has_ig", "Not Found", ig_url="https://instagram.com/valid_creator")
    _seed_creator(db, "yt_not_found", "Not Found", ig_url="Not Found")
    _seed_creator(db, "yt_empty_ig", "Not Found", ig_url="")

    wf = InstagramDMWorkflow(db)
    queue = wf.sync_queue(db.get_messages(), run_id="run-dm")
    by_id = {q.influencer_id: q for q in queue}

    assert by_id["yt_has_ig"].status == DMStatus.PENDING_MANUAL
    assert by_id["yt_not_found"].status == DMStatus.SKIPPED_NO_HANDLE
    assert by_id["yt_not_found"].last_event_status == DMStatus.SKIPPED_NO_HANDLE
    assert by_id["yt_not_found"].sent is False
    assert len(by_id["yt_not_found"].instagram_dm.split()) >= 15
    assert by_id["yt_empty_ig"].status == DMStatus.SKIPPED_NO_HANDLE
    assert by_id["yt_empty_ig"].sent is False

    # Calling simulate_send on valid IG sends cleanly; calling it on Not Found / empty stays Skipped
    valid_res = wf.simulate_send("yt_has_ig")
    nf_res = wf.simulate_send("yt_not_found")
    empty_res = wf.mark_sent_manually("yt_empty_ig")
    assert valid_res is not None and valid_res.status == DMStatus.SIMULATED_SENT and valid_res.sent is True
    assert nf_res is not None and nf_res.status == DMStatus.SKIPPED_NO_HANDLE and nf_res.sent is False
    assert empty_res is not None and empty_res.status == DMStatus.SKIPPED_NO_HANDLE and empty_res.sent is False

    # Future sync_queue must never promote Not Found / empty into Simulated Sent
    resynced = {q.influencer_id: q for q in wf.sync_queue(db.get_messages(), run_id="run-dm-2")}
    assert resynced["yt_not_found"].status == DMStatus.SKIPPED_NO_HANDLE
    assert resynced["yt_not_found"].sent is False
    assert resynced["yt_has_ig"].sent is True
    assert resynced["yt_has_ig"].run_id == "run-dm"

    # Simulate legacy bug row (sent=1 with Not Found) and verify DB init reconciles without deleting history
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE dm_queue SET sent = 1, status = 'Simulated Sent', last_event_status = 'Simulated Sent' WHERE influencer_id = 'yt_not_found'"
    )
    conn.commit()
    conn.close()

    prior_log_count = len(db.get_dm_dispatch_log("yt_not_found"))
    reopened = OutreachDatabase(db_path)
    fixed = next(q for q in reopened.get_dm_queue(current_only=False) if q.influencer_id == "yt_not_found")
    assert fixed.sent is False
    assert fixed.sent_date is None
    assert fixed.status == DMStatus.SKIPPED_NO_HANDLE
    assert fixed.last_event_status == DMStatus.SKIPPED_NO_HANDLE
    after_logs = reopened.get_dm_dispatch_log("yt_not_found")
    assert len(after_logs) == prior_log_count + 1
    assert after_logs[0]["status"] == DMStatus.SKIPPED_NO_HANDLE.value
    assert after_logs[0]["sent"] == 0


def test_stale_sending_crash_recovery_preserves_active_and_allows_safe_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "crash_recovery.db"
    out_dir = tmp_path / "outputs"
    settings = Settings(
        database_path=str(db_path),
        outputs_dir=str(out_dir),
        send_mode="simulate",
        stale_sending_timeout_seconds=300,
        allow_live_smtp=False,
    )
    db = OutreachDatabase(db_path, stale_sending_timeout_seconds=settings.stale_sending_timeout_seconds)

    p_stale_sim = _seed_creator(db, "yt_stale_sim", "stale_sim@creator.io")
    p_active = _seed_creator(db, "yt_active", "active@creator.io")
    p_stale_smtp = _seed_creator(db, "yt_stale_smtp", "stale_smtp@creator.io")
    p_done = _seed_creator(db, "yt_done", "done@creator.io")

    # Complete one real simulated send first so we verify completed duplicate blocks survive recovery
    sender = EmailOutreachSender(settings, db)
    done_res = sender.dispatch_single(p_done, mode="simulate")
    assert done_res.status == EmailSendStatus.SENT_SIMULATED

    # Acquire in-flight Sending claims without finalizing (simulates worker crashes / active send)
    claimed_sim, id_stale_sim, _ = db.claim_outreach_slot(
        run_id="run-1",
        influencer_id=p_stale_sim.id,
        influencer_name=p_stale_sim.name,
        email=p_stale_sim.contact_email,
        email_subject="Subject",
        send_mode="simulate",
        receipt_id="sim-crashed",
    )
    claimed_active, id_active, _ = db.claim_outreach_slot(
        run_id="run-1",
        influencer_id=p_active.id,
        influencer_name=p_active.name,
        email=p_active.contact_email,
        email_subject="Subject",
        send_mode="simulate",
        receipt_id="sim-active",
    )
    claimed_smtp, id_stale_smtp, _ = db.claim_outreach_slot(
        run_id="run-1",
        influencer_id=p_stale_smtp.id,
        influencer_name=p_stale_smtp.name,
        email=p_stale_smtp.contact_email,
        email_subject="Subject",
        send_mode="smtp",
        receipt_id="smtp-crashed",
    )
    assert claimed_sim and claimed_active and claimed_smtp

    # Verify in-flight Sending rows do NOT falsely report sent=1 before finalization
    in_flight = {r.id: r for r in db.get_outreach_log()}
    assert in_flight[id_active].sent is False
    assert in_flight[id_active].status == EmailSendStatus.SENDING

    # Backdate the two crashed slots by 10 minutes; keep active slot 10 seconds old
    old_ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    recent_ts = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE outreach_log SET created_at = ? WHERE id IN (?, ?)", (old_ts, id_stale_sim, id_stale_smtp))
    conn.execute("UPDATE outreach_log SET created_at = ? WHERE id = ?", (recent_ts, id_active))
    conn.commit()
    conn.close()

    # Ensure recovery never invokes SMTP or sends emails automatically
    mock_smtp = MagicMock()
    monkeypatch.setattr("outreach_system.sending.email_sender.smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("outreach_system.sending.email_sender.smtplib.SMTP_SSL", mock_smtp)

    recovered_db = OutreachDatabase(db_path, stale_sending_timeout_seconds=300)
    logs_by_id = {r.id: r for r in recovered_db.get_outreach_log()}

    assert logs_by_id[id_stale_sim].status == EmailSendStatus.FAILED
    assert logs_by_id[id_stale_sim].sent is False
    assert logs_by_id[id_stale_sim].receipt_id == ""
    assert logs_by_id[id_stale_smtp].status == EmailSendStatus.MANUAL_VERIFICATION_REQUIRED
    assert logs_by_id[id_stale_smtp].sent is False
    assert logs_by_id[id_stale_smtp].receipt_id == "smtp-crashed"
    assert "uncertain" in logs_by_id[id_stale_smtp].notes.lower()
    assert logs_by_id[id_active].status == EmailSendStatus.SENDING
    mock_smtp.assert_not_called()

    # Safe explicit retry succeeds for stale simulate claim, blocks duplicate on done,
    # AND blocks automatic retry on uncertain SMTP row until operator verifies!
    recovered_sender = EmailOutreachSender(settings, recovered_db)
    retry_res = recovered_sender.dispatch_single(p_stale_sim, mode="simulate")
    assert retry_res.status == EmailSendStatus.SENT_SIMULATED
    assert retry_res.sent is True
    assert recovered_sender.dispatch_single(p_stale_sim, mode="simulate").status == EmailSendStatus.SKIPPED_DUPLICATE
    assert recovered_sender.dispatch_single(p_done, mode="simulate").status == EmailSendStatus.SKIPPED_DUPLICATE
    blocked_smtp = recovered_sender.dispatch_single(p_stale_smtp, mode="simulate")
    assert blocked_smtp.status == EmailSendStatus.SKIPPED_VERIFICATION_PENDING
    assert blocked_smtp.sent is False
    mock_smtp.assert_not_called()


def test_uncertain_smtp_delivery_vs_presend_failure_and_operator_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import smtplib

    db_path = tmp_path / "smtp_uncertain.db"
    settings = Settings(
        database_path=str(db_path),
        outputs_dir=str(tmp_path / "outputs"),
        send_mode="smtp",
        allow_live_smtp=True,
        smtp_host="smtp.example.net",
        smtp_port=587,
        smtp_user="u@example.net",
        smtp_password="pw",
    )
    db = OutreachDatabase(db_path)
    p_presend = _seed_creator(db, "yt_pre", "presend@creator.io")
    p_uncertain_yes = _seed_creator(db, "yt_unc_y", "yes@creator.io")
    p_uncertain_no = _seed_creator(db, "yt_unc_n", "no@creator.io")

    sender = EmailOutreachSender(settings, db)

    # 1. Pre-send failure (e.g. auth failure) guarantees not sent -> FAILED & retryable
    monkeypatch.setattr(
        sender,
        "_send_via_smtp",
        MagicMock(side_effect=smtplib.SMTPAuthenticationError(535, b"Auth failed")),
    )
    res_pre = sender.dispatch_single(p_presend, mode="smtp")
    assert res_pre.status == EmailSendStatus.FAILED
    assert res_pre.sent is False
    assert res_pre.receipt_id == ""

    # 2. Uncertain mid-transmission exception -> Verify Delivery - Uncertain SMTP, locks auto-retry
    monkeypatch.setattr(
        sender,
        "_send_via_smtp",
        MagicMock(side_effect=smtplib.SMTPServerDisconnected("Connection lost after DATA")),
    )
    res_unc1 = sender.dispatch_single(p_uncertain_yes, mode="smtp")
    assert res_unc1.status == EmailSendStatus.MANUAL_VERIFICATION_REQUIRED
    assert res_unc1.sent is False
    assert res_unc1.receipt_id.startswith("smtp-")

    # Auto-retry must refuse to resend while verification is pending!
    auto_retry_blocked = sender.dispatch_single(p_uncertain_yes, mode="smtp")
    assert auto_retry_blocked.status == EmailSendStatus.SKIPPED_VERIFICATION_PENDING
    assert auto_retry_blocked.sent is False

    # Operator confirms message actually arrived -> SENT_SMTP, duplicate permanently blocked
    confirmed_sent = db.confirm_uncertain_smtp_outcome(
        res_unc1.id, delivered=True, operator_note="Confirmed in Sent folder"
    )
    assert confirmed_sent.status == EmailSendStatus.SENT_SMTP
    assert confirmed_sent.sent is True
    assert confirmed_sent.sent_date is not None
    assert sender.dispatch_single(p_uncertain_yes, mode="smtp").status == EmailSendStatus.SKIPPED_DUPLICATE

    # 3. Uncertain failure later confirmed NOT sent -> FAILED & unlocked for safe explicit retry
    res_unc2 = sender.dispatch_single(p_uncertain_no, mode="smtp")
    assert res_unc2.status == EmailSendStatus.MANUAL_VERIFICATION_REQUIRED
    confirmed_fail = db.confirm_uncertain_smtp_outcome(
        res_unc2.id, delivered=False, operator_note="Bounce log confirmed not accepted"
    )
    assert confirmed_fail.status == EmailSendStatus.FAILED
    assert confirmed_fail.sent is False
    assert confirmed_fail.receipt_id == ""
    assert "not accepted" in confirmed_fail.notes

    monkeypatch.setattr(sender, "_send_via_smtp", MagicMock(return_value=None))
    retried = sender.dispatch_single(p_uncertain_no, mode="smtp")
    assert retried.status == EmailSendStatus.SENT_SMTP
    assert retried.sent is True
