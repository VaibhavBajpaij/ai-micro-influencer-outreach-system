"""SQLite storage repository with WAL mode, run-scoped isolation, and atomic duplicate protection."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from outreach_system.models import (
    DMQueueEntry,
    DMStatus,
    EmailSendStatus,
    FilterStatus,
    InfluencerProfile,
    OutreachLogEntry,
    PersonalizedMessage,
    PipelineSummary,
    has_valid_instagram_target,
    resolve_generation_mode,
)


class OutreachDatabase:
    """Thread-safe SQLite repository for all pipeline artifacts and outreach logs."""

    _init_lock = threading.Lock()
    _initialized_signatures: dict[Path, tuple[int, int]] = {}

    def __init__(self, db_path: str | Path, *, stale_sending_timeout_seconds: int = 300) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.stale_sending_timeout_seconds = stale_sending_timeout_seconds
        resolved = self.db_path.resolve()
        with self._init_lock:
            sig = self._file_signature(resolved)
            if sig is None or self._initialized_signatures.get(resolved) != sig:
                self._init_schema()
                new_sig = self._file_signature(resolved)
                if new_sig is not None:
                    self._initialized_signatures[resolved] = new_sig
        self.reconcile_invalid_dm_entries()
        self.reconcile_stale_sending_slots(
            stale_after_seconds=self.stale_sending_timeout_seconds
        )

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
            return (st.st_ino, st.st_ctime_ns)
        except OSError:
            return None

    def checkpoint(self) -> None:
        """Flush WAL frames into the main SQLite file so single-file copies are self-contained."""
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        isolation = None if immediate else ""
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=isolation)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        if immediate:
            conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(r["name"]) for r in rows}

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS influencers (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL DEFAULT '',
                    is_current INTEGER NOT NULL DEFAULT 1,
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

                CREATE TABLE IF NOT EXISTS outreach_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL DEFAULT '',
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

                CREATE TABLE IF NOT EXISTS dm_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL DEFAULT '',
                    is_current INTEGER NOT NULL DEFAULT 1,
                    influencer_id TEXT NOT NULL UNIQUE,
                    influencer_name TEXT NOT NULL,
                    instagram_url TEXT NOT NULL,
                    instagram_dm TEXT NOT NULL,
                    dm_word_count INTEGER NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    sent_date TEXT,
                    status TEXT NOT NULL,
                    last_event_status TEXT NOT NULL DEFAULT 'Pending Manual Send',
                    duplicate_attempts INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (influencer_id) REFERENCES influencers(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS dm_dispatch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL DEFAULT '',
                    influencer_id TEXT NOT NULL,
                    influencer_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY,
                    niche TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    executed_at TEXT NOT NULL
                );
                """
            )

            # Migrate pre-existing tables created before run-scoped auditing & constraints
            inf_cols = self._table_columns(conn, "influencers")
            if "run_id" not in inf_cols:
                conn.execute("ALTER TABLE influencers ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
            if "is_current" not in inf_cols:
                conn.execute("ALTER TABLE influencers ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1")

            log_cols = self._table_columns(conn, "outreach_log")
            if "run_id" not in log_cols:
                conn.execute("ALTER TABLE outreach_log ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")

            dm_cols = self._table_columns(conn, "dm_queue")
            if "run_id" not in dm_cols:
                conn.execute("ALTER TABLE dm_queue ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
            if "is_current" not in dm_cols:
                conn.execute("ALTER TABLE dm_queue ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1")
            if "last_event_status" not in dm_cols:
                conn.execute(
                    "ALTER TABLE dm_queue ADD COLUMN last_event_status TEXT NOT NULL DEFAULT 'Pending Manual Send'"
                )
            if "duplicate_attempts" not in dm_cols:
                conn.execute(
                    "ALTER TABLE dm_queue ADD COLUMN duplicate_attempts INTEGER NOT NULL DEFAULT 0"
                )

            # Ensure personalized_messages supports (run_id, influencer_id) composite PK for history preservation
            msg_cols = self._table_columns(conn, "personalized_messages")
            if not msg_cols:
                conn.execute(
                    """
                    CREATE TABLE personalized_messages (
                        run_id TEXT NOT NULL DEFAULT '',
                        influencer_id TEXT NOT NULL,
                        is_current INTEGER NOT NULL DEFAULT 1,
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
                        generation_mode TEXT NOT NULL DEFAULT 'contextual_fallback',
                        llm_provider TEXT NOT NULL,
                        llm_model TEXT NOT NULL,
                        generated_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, influencer_id),
                        FOREIGN KEY (influencer_id) REFERENCES influencers(id) ON DELETE CASCADE
                    )
                    """
                )
            elif "run_id" not in msg_cols or "is_current" not in msg_cols:
                conn.executescript(
                    """
                    ALTER TABLE personalized_messages RENAME TO personalized_messages_legacy;
                    CREATE TABLE personalized_messages (
                        run_id TEXT NOT NULL DEFAULT '',
                        influencer_id TEXT NOT NULL,
                        is_current INTEGER NOT NULL DEFAULT 1,
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
                        generation_mode TEXT NOT NULL DEFAULT 'contextual_fallback',
                        llm_provider TEXT NOT NULL,
                        llm_model TEXT NOT NULL,
                        generated_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, influencer_id),
                        FOREIGN KEY (influencer_id) REFERENCES influencers(id) ON DELETE CASCADE
                    );
                    INSERT INTO personalized_messages (
                        run_id, influencer_id, is_current, influencer_name, platform, contact_email,
                        instagram_url, collaboration_angle, email_subject, email_pitch, email_word_count,
                        instagram_dm, dm_word_count, signals_json, generation_mode, llm_provider, llm_model, generated_at
                    )
                    SELECT
                        'legacy', influencer_id, 1, influencer_name, platform, contact_email,
                        instagram_url, collaboration_angle, email_subject, email_pitch, email_word_count,
                        instagram_dm, dm_word_count, signals_json,
                        CASE
                            WHEN lower(llm_provider) IN ('gemini', 'openai', 'groq', 'anthropic', 'ollama') THEN 'LLM'
                            ELSE 'contextual_fallback'
                        END,
                        llm_provider, llm_model, generated_at
                    FROM personalized_messages_legacy;
                    DROP TABLE personalized_messages_legacy;
                    """
                )
            else:
                msg_cols = self._table_columns(conn, "personalized_messages")
                if "generation_mode" not in msg_cols:
                    conn.execute(
                        "ALTER TABLE personalized_messages ADD COLUMN generation_mode TEXT NOT NULL DEFAULT 'contextual_fallback'"
                    )
                    conn.execute(
                        """
                        UPDATE personalized_messages
                        SET generation_mode = CASE
                            WHEN lower(llm_provider) IN ('gemini', 'openai', 'groq', 'anthropic', 'ollama') THEN 'LLM'
                            ELSE 'contextual_fallback'
                        END
                        """
                    )

            idx_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'uq_outreach_sent_influencer'"
            ).fetchone()
            if idx_row and "Verify Delivery - Uncertain SMTP" not in str(idx_row["sql"] or ""):
                conn.executescript(
                    """
                    DROP INDEX IF EXISTS uq_outreach_sent_influencer;
                    DROP INDEX IF EXISTS uq_outreach_sent_email;
                    """
                )

            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_influencers_status ON influencers(status, is_current);
                CREATE INDEX IF NOT EXISTS idx_influencers_platform ON influencers(platform);
                CREATE INDEX IF NOT EXISTS idx_influencers_run ON influencers(run_id);
                CREATE INDEX IF NOT EXISTS idx_messages_current ON personalized_messages(is_current, run_id);
                CREATE INDEX IF NOT EXISTS idx_outreach_influencer ON outreach_log(influencer_id, sent);
                CREATE INDEX IF NOT EXISTS idx_outreach_email_norm ON outreach_log(email_normalized, sent);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_sent_influencer
                ON outreach_log(influencer_id)
                WHERE status IN ('Sending', 'Sent (Simulated)', 'Sent (SMTP)', 'Verify Delivery - Uncertain SMTP');

                CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_sent_email
                ON outreach_log(email_normalized)
                WHERE email_normalized != '' AND status IN ('Sending', 'Sent (Simulated)', 'Sent (SMTP)', 'Verify Delivery - Uncertain SMTP');
                """
            )

    def reconcile_invalid_dm_entries(self) -> int:
        """Correct DM rows marked sent/pending without a valid Instagram handle, preserving audit history."""
        with self._connect() as conn:
            return self._reconcile_invalid_dm_entries(conn)

    def _reconcile_invalid_dm_entries(self, conn: sqlite3.Connection) -> int:
        """Correct DM rows marked sent/pending without a valid Instagram handle, preserving audit history."""
        rows = conn.execute(
            """
            SELECT * FROM dm_queue
            WHERE sent = 1 OR status IN ('Simulated Sent', 'Marked Sent Manually', 'Pending Manual Send')
            """
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        corrected = 0
        for row in rows:
            if has_valid_instagram_target(row["instagram_url"]):
                continue
            was_sent = bool(row["sent"] == 1 or row["status"] in ("Simulated Sent", "Marked Sent Manually"))
            queue_note = "No public Instagram handle/URL found; DM skipped (draft copy retained for manual lookup)."
            log_note = (
                "Reconciled historical DM status: no public Instagram handle/URL found "
                "(prior erroneous delivery status superseded without deleting audit history)."
                if was_sent
                else queue_note
            )
            conn.execute(
                """
                UPDATE dm_queue
                SET sent = 0,
                    sent_date = NULL,
                    status = ?,
                    last_event_status = ?,
                    notes = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    DMStatus.SKIPPED_NO_HANDLE.value,
                    DMStatus.SKIPPED_NO_HANDLE.value,
                    queue_note,
                    now,
                    row["id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO dm_dispatch_log (run_id, influencer_id, influencer_name, status, sent, notes, created_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    row["run_id"],
                    row["influencer_id"],
                    row["influencer_name"],
                    DMStatus.SKIPPED_NO_HANDLE.value,
                    log_note,
                    now,
                ),
            )
            corrected += 1
        return corrected

    def upsert_influencers(
        self,
        profiles: list[InfluencerProfile],
        run_id: str | None = None,
        mark_previous_inactive: bool = True,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        active_run = run_id or (profiles[0].run_id if profiles and profiles[0].run_id else "")
        with self._connect(immediate=True) as conn:
            if mark_previous_inactive:
                conn.execute("UPDATE influencers SET is_current = 0")
            conn.executemany(
                """
                INSERT INTO influencers (
                    id, run_id, is_current, name, handle, platform, profile_url, followers, engagement_rate,
                    niche, contact_email, status, tier, filter_score, audience_geography,
                    profile_json, updated_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    run_id = excluded.run_id,
                    is_current = 1,
                    name = excluded.name,
                    handle = excluded.handle,
                    platform = excluded.platform,
                    profile_url = excluded.profile_url,
                    followers = excluded.followers,
                    engagement_rate = excluded.engagement_rate,
                    niche = excluded.niche,
                    contact_email = excluded.contact_email,
                    status = excluded.status,
                    tier = excluded.tier,
                    filter_score = excluded.filter_score,
                    audience_geography = excluded.audience_geography,
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        p.id,
                        p.run_id or active_run,
                        p.name,
                        p.handle,
                        p.platform,
                        p.profile_url,
                        p.followers,
                        p.engagement_rate,
                        p.niche,
                        p.contact_email,
                        p.status.value,
                        p.tier,
                        p.filter_score,
                        p.audience_geography,
                        p.model_dump_json(),
                        now,
                    )
                    for p in profiles
                ],
            )

    def get_current_run_id(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM influencers WHERE is_current = 1 AND run_id != '' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return str(row["run_id"])
            run_row = conn.execute(
                "SELECT run_id FROM pipeline_runs ORDER BY executed_at DESC LIMIT 1"
            ).fetchone()
            return str(run_row["run_id"]) if run_row else ""

    def get_influencers(
        self,
        status: FilterStatus | None = None,
        current_only: bool = True,
    ) -> list[InfluencerProfile]:
        clauses: list[str] = []
        params: list[str] = []
        if current_only:
            clauses.append("is_current = 1")
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            f"SELECT profile_json FROM influencers{where_sql} "
            "ORDER BY CASE WHEN status = 'Qualified' THEN 0 ELSE 1 END, filter_score DESC, COALESCE(followers, 0) DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [InfluencerProfile.model_validate_json(r["profile_json"]) for r in rows]

    def get_influencer(self, influencer_id: str) -> InfluencerProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT profile_json FROM influencers WHERE id = ?", (influencer_id,)
            ).fetchone()
        return InfluencerProfile.model_validate_json(row["profile_json"]) if row else None

    def save_run_messages(
        self,
        messages: list[PersonalizedMessage],
        run_id: str,
        *,
        replace_run: bool = True,
    ) -> None:
        """Persist run-scoped messages while archiving prior runs for auditing."""
        with self._connect(immediate=True) as conn:
            if replace_run:
                conn.execute("UPDATE personalized_messages SET is_current = 0")
            else:
                conn.execute("UPDATE personalized_messages SET is_current = 0 WHERE run_id != ?", (run_id,))
            if not messages:
                return
            conn.executemany(
                """
                INSERT INTO personalized_messages (
                    run_id, influencer_id, is_current, influencer_name, platform, contact_email,
                    instagram_url, collaboration_angle, email_subject, email_pitch, email_word_count,
                    instagram_dm, dm_word_count, signals_json, generation_mode, llm_provider, llm_model, generated_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, influencer_id) DO UPDATE SET
                    is_current = 1,
                    influencer_name = excluded.influencer_name,
                    platform = excluded.platform,
                    contact_email = excluded.contact_email,
                    instagram_url = excluded.instagram_url,
                    collaboration_angle = excluded.collaboration_angle,
                    email_subject = excluded.email_subject,
                    email_pitch = excluded.email_pitch,
                    email_word_count = excluded.email_word_count,
                    instagram_dm = excluded.instagram_dm,
                    dm_word_count = excluded.dm_word_count,
                    signals_json = excluded.signals_json,
                    generation_mode = excluded.generation_mode,
                    llm_provider = excluded.llm_provider,
                    llm_model = excluded.llm_model,
                    generated_at = excluded.generated_at
                """,
                [
                    (
                        m.run_id or run_id,
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
                        json.dumps(m.personalization_signals_used),
                        m.generation_mode or resolve_generation_mode(m.llm_provider),
                        m.llm_provider,
                        m.llm_model,
                        m.generated_at,
                    )
                    for m in messages
                ],
            )

    def upsert_messages(self, messages: list[PersonalizedMessage]) -> None:
        """Incremental wrapper that scopes messages to their run_id without clearing same-run peers."""
        if not messages:
            return
        run_id = messages[0].run_id or self.get_current_run_id() or "default"
        self.save_run_messages(messages, run_id=run_id, replace_run=False)

    def get_messages(self, current_only: bool = True, run_id: str | None = None) -> list[PersonalizedMessage]:
        with self._connect() as conn:
            if run_id is not None:
                rows = conn.execute(
                    """
                    SELECT m.* FROM personalized_messages m
                    WHERE m.run_id = ?
                    ORDER BY m.generated_at DESC, m.influencer_name ASC
                    """,
                    (run_id,),
                ).fetchall()
            elif current_only:
                rows = conn.execute(
                    """
                    SELECT m.* FROM personalized_messages m
                    JOIN influencers i ON m.influencer_id = i.id
                    WHERE m.is_current = 1 AND i.is_current = 1 AND i.status = 'Qualified'
                    ORDER BY m.generated_at DESC, m.influencer_name ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM personalized_messages ORDER BY generated_at DESC, influencer_name ASC"
                ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def get_message_for_influencer(self, influencer_id: str) -> PersonalizedMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT m.* FROM personalized_messages m
                JOIN influencers i ON m.influencer_id = i.id
                WHERE m.influencer_id = ? AND m.is_current = 1 AND i.is_current = 1
                ORDER BY m.generated_at DESC LIMIT 1
                """,
                (influencer_id,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT * FROM personalized_messages
                    WHERE influencer_id = ? AND is_current = 1
                    ORDER BY generated_at DESC LIMIT 1
                    """,
                    (influencer_id,),
                ).fetchone()
        return self._row_to_message(row) if row else None

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> PersonalizedMessage:
        keys = row.keys()
        gen_mode = (
            row["generation_mode"]
            if "generation_mode" in keys and row["generation_mode"]
            else resolve_generation_mode(row["llm_provider"])
        )
        return PersonalizedMessage(
            run_id=row["run_id"],
            influencer_id=row["influencer_id"],
            influencer_name=row["influencer_name"],
            platform=row["platform"],
            contact_email=row["contact_email"],
            instagram_url=row["instagram_url"],
            collaboration_angle=row["collaboration_angle"],
            email_subject=row["email_subject"],
            email_pitch=row["email_pitch"],
            email_word_count=row["email_word_count"],
            instagram_dm=row["instagram_dm"],
            dm_word_count=row["dm_word_count"],
            personalization_signals_used=json.loads(row["signals_json"]),
            generation_mode=gen_mode,
            llm_provider=row["llm_provider"],
            llm_model=row["llm_model"],
            generated_at=row["generated_at"],
        )

    def has_sent_outreach(self, influencer_id: str, email: str) -> bool:
        """Return True if an active, completed, or verification-pending outreach exists."""
        email_norm = email.strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM outreach_log
                WHERE status IN ('Sending', 'Sent (Simulated)', 'Sent (SMTP)', 'Verify Delivery - Uncertain SMTP')
                  AND (influencer_id = ? OR (email_normalized != '' AND email_normalized = ?))
                LIMIT 1
                """,
                (influencer_id, email_norm),
            ).fetchone()
        return row is not None

    def claim_outreach_slot(
        self,
        *,
        run_id: str,
        influencer_id: str,
        influencer_name: str,
        email: str,
        email_subject: str,
        send_mode: str,
        receipt_id: str,
    ) -> tuple[bool, int, OutreachLogEntry | None]:
        """Atomically claim a send slot under SQLite BEGIN IMMEDIATE + partial unique index."""
        now = datetime.now(UTC).isoformat()
        email_norm = "" if email.lower() == "not found" else email.strip().lower()
        with self._connect(immediate=True) as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO outreach_log (
                        run_id, influencer_id, influencer_name, email, email_normalized,
                        message_generated, email_subject, sent, sent_date, status,
                        send_mode, receipt_id, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, ?, 0, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        influencer_id,
                        influencer_name,
                        email,
                        email_norm,
                        email_subject,
                        EmailSendStatus.SENDING.value,
                        send_mode,
                        receipt_id,
                        "Atomic outreach lock acquired.",
                        now,
                    ),
                )
                return True, int(cur.lastrowid or 0), None
            except sqlite3.IntegrityError:
                blocker = conn.execute(
                    """
                    SELECT status, receipt_id FROM outreach_log
                    WHERE status IN ('Sending', 'Sent (Simulated)', 'Sent (SMTP)', 'Verify Delivery - Uncertain SMTP')
                      AND (influencer_id = ? OR (email_normalized != '' AND email_normalized = ?))
                    ORDER BY id DESC LIMIT 1
                    """,
                    (influencer_id, email_norm),
                ).fetchone()
                pending_verify = (
                    blocker is not None
                    and blocker["status"] == EmailSendStatus.MANUAL_VERIFICATION_REQUIRED.value
                )
                skip_status = (
                    EmailSendStatus.SKIPPED_VERIFICATION_PENDING
                    if pending_verify
                    else EmailSendStatus.SKIPPED_DUPLICATE
                )
                skip_note = (
                    f"Automatic retry blocked: prior SMTP attempt (receipt {blocker['receipt_id'] or 'N/A'}) "
                    "requires manual delivery verification before retrying."
                    if pending_verify
                    else "Duplicate outreach blocked atomically: creator or email already contacted."
                )
                cur = conn.execute(
                    """
                    INSERT INTO outreach_log (
                        run_id, influencer_id, influencer_name, email, email_normalized,
                        message_generated, email_subject, sent, sent_date, status,
                        send_mode, receipt_id, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, ?, 0, NULL, ?, ?, '', ?, ?)
                    """,
                    (
                        run_id,
                        influencer_id,
                        influencer_name,
                        email,
                        email_norm,
                        email_subject,
                        skip_status.value,
                        send_mode,
                        skip_note,
                        now,
                    ),
                )
                dup_id = int(cur.lastrowid or 0)
                return (
                    False,
                    dup_id,
                    OutreachLogEntry(
                        id=dup_id,
                        run_id=run_id,
                        influencer_id=influencer_id,
                        influencer_name=influencer_name,
                        email=email,
                        message_generated=True,
                        email_subject=email_subject,
                        sent=False,
                        sent_date=None,
                        status=skip_status,
                        send_mode=send_mode,
                        receipt_id="",
                        notes=skip_note,
                    ),
                )

    def finalize_outreach_slot(
        self,
        log_id: int,
        *,
        status: EmailSendStatus,
        sent: bool,
        sent_date: str | None,
        receipt_id: str,
        notes: str,
    ) -> OutreachLogEntry:
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE outreach_log
                SET status = ?, sent = ?, sent_date = ?, receipt_id = ?, notes = ?
                WHERE id = ?
                """,
                (status.value, 1 if sent else 0, sent_date, receipt_id, notes, log_id),
            )
            r = conn.execute("SELECT * FROM outreach_log WHERE id = ?", (log_id,)).fetchone()
        return self._row_to_outreach_entry(r)

    @staticmethod
    def _is_row_stale(created_at_raw: str | None, ref_now: datetime, stale_after_seconds: int) -> bool:
        if not created_at_raw:
            return True
        try:
            dt = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return (ref_now - dt).total_seconds() > stale_after_seconds
        except ValueError:
            return True

    def reconcile_stale_sending_slots(
        self,
        stale_after_seconds: int = 300,
        *,
        now: datetime | None = None,
    ) -> list[OutreachLogEntry]:
        """Reconcile crashed/timed-out Sending claims without auto-resending uncertain deliveries."""
        ref_now = now or datetime.now(UTC)
        with self._connect() as conn:
            in_flight = conn.execute(
                "SELECT id, created_at FROM outreach_log WHERE status = ?",
                (EmailSendStatus.SENDING.value,),
            ).fetchall()
        candidate_ids = [
            int(r["id"])
            for r in in_flight
            if self._is_row_stale(r["created_at"], ref_now, stale_after_seconds)
        ]
        if not candidate_ids:
            return []

        reconciled: list[OutreachLogEntry] = []
        with self._connect(immediate=True) as conn:
            for row_id in candidate_ids:
                row = conn.execute(
                    "SELECT * FROM outreach_log WHERE id = ? AND status = ?",
                    (row_id, EmailSendStatus.SENDING.value),
                ).fetchone()
                if not row or not self._is_row_stale(row["created_at"], ref_now, stale_after_seconds):
                    continue
                if str(row["send_mode"]).lower() == "smtp":
                    next_status = EmailSendStatus.MANUAL_VERIFICATION_REQUIRED.value
                    next_receipt = str(row["receipt_id"] or "")
                    note = (
                        f"Stale in-flight SMTP claim after {stale_after_seconds}s timeout/crash (delivery uncertain). "
                        f"Receipt {next_receipt or 'N/A'} preserved; operator must confirm no delivery occurred before retry."
                    )
                else:
                    next_status = EmailSendStatus.FAILED.value
                    next_receipt = ""
                    note = (
                        f"Reconciled stale in-flight simulate Sending claim after {stale_after_seconds}s timeout/crash "
                        "(never finalized; marked Failed so retry can proceed safely)."
                    )
                conn.execute(
                    """
                    UPDATE outreach_log
                    SET status = ?, sent = 0, sent_date = NULL, receipt_id = ?, notes = ?
                    WHERE id = ?
                    """,
                    (next_status, next_receipt, note, row_id),
                )
                updated = conn.execute("SELECT * FROM outreach_log WHERE id = ?", (row_id,)).fetchone()
                if updated:
                    reconciled.append(self._row_to_outreach_entry(updated))
        return reconciled

    def get_outreach_entry(self, log_id: int) -> OutreachLogEntry | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM outreach_log WHERE id = ?", (log_id,)).fetchone()
        return self._row_to_outreach_entry(row) if row else None

    def confirm_uncertain_smtp_outcome(
        self,
        log_id: int,
        *,
        delivered: bool,
        operator_note: str = "",
    ) -> OutreachLogEntry | None:
        """Resolve a pending uncertain SMTP record after explicit operator verification."""
        now = datetime.now(UTC).isoformat()
        with self._connect(immediate=True) as conn:
            row = conn.execute("SELECT * FROM outreach_log WHERE id = ?", (log_id,)).fetchone()
            if not row:
                return None
            if row["status"] != EmailSendStatus.MANUAL_VERIFICATION_REQUIRED.value:
                raise ValueError(
                    f"Outreach record {log_id} has status '{row['status']}', not pending SMTP verification."
                )
            preserved_receipt = str(row["receipt_id"] or "")
            extra = f" Note: {operator_note.strip()}" if operator_note.strip() else ""
            if delivered:
                next_status = EmailSendStatus.SENT_SMTP.value
                next_receipt = preserved_receipt
                sent_val = 1
                sent_date = now
                note = (
                    f"Operator confirmed uncertain SMTP attempt was delivered (receipt {preserved_receipt or 'N/A'}).{extra}"
                )
            else:
                next_status = EmailSendStatus.FAILED.value
                next_receipt = ""
                sent_val = 0
                sent_date = None
                note = (
                    f"Operator confirmed no delivery occurred for SMTP receipt {preserved_receipt or 'N/A'}; safe to retry.{extra}"
                )
            conn.execute(
                """
                UPDATE outreach_log
                SET status = ?, sent = ?, sent_date = ?, receipt_id = ?, notes = ?
                WHERE id = ?
                """,
                (next_status, sent_val, sent_date, next_receipt, note, log_id),
            )
            updated = conn.execute("SELECT * FROM outreach_log WHERE id = ?", (log_id,)).fetchone()
        return self._row_to_outreach_entry(updated) if updated else None

    def append_outreach_log(self, entry: OutreachLogEntry) -> int:
        now = datetime.now(UTC).isoformat()
        email_norm = "" if entry.email.lower() == "not found" else entry.email.strip().lower()
        with self._connect(immediate=True) as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO outreach_log (
                        run_id, influencer_id, influencer_name, email, email_normalized,
                        message_generated, email_subject, sent, sent_date, status,
                        send_mode, receipt_id, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.run_id,
                        entry.influencer_id,
                        entry.influencer_name,
                        entry.email,
                        email_norm,
                        1 if entry.message_generated else 0,
                        entry.email_subject,
                        1 if entry.sent else 0,
                        entry.sent_date,
                        entry.status.value,
                        entry.send_mode,
                        entry.receipt_id,
                        entry.notes,
                        now,
                    ),
                )
                return int(cur.lastrowid or 0)
            except sqlite3.IntegrityError:
                cur = conn.execute(
                    """
                    INSERT INTO outreach_log (
                        run_id, influencer_id, influencer_name, email, email_normalized,
                        message_generated, email_subject, sent, sent_date, status,
                        send_mode, receipt_id, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, '', ?, ?)
                    """,
                    (
                        entry.run_id,
                        entry.influencer_id,
                        entry.influencer_name,
                        entry.email,
                        email_norm,
                        1 if entry.message_generated else 0,
                        entry.email_subject,
                        EmailSendStatus.SKIPPED_DUPLICATE.value,
                        entry.send_mode,
                        "Duplicate outreach blocked by database constraint.",
                        now,
                    ),
                )
                return int(cur.lastrowid or 0)

    def get_outreach_log(self, run_id: str | None = None) -> list[OutreachLogEntry]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM outreach_log WHERE run_id = ? ORDER BY id DESC", (run_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM outreach_log ORDER BY id DESC").fetchall()
        return [self._row_to_outreach_entry(r) for r in rows]

    @staticmethod
    def _row_to_outreach_entry(r: sqlite3.Row) -> OutreachLogEntry:
        return OutreachLogEntry(
            id=r["id"],
            run_id=r["run_id"],
            influencer_id=r["influencer_id"],
            influencer_name=r["influencer_name"],
            email=r["email"],
            message_generated=bool(r["message_generated"]),
            email_subject=r["email_subject"],
            sent=bool(r["sent"]),
            sent_date=r["sent_date"],
            status=EmailSendStatus(r["status"]),
            send_mode=r["send_mode"],
            receipt_id=r["receipt_id"],
            notes=r["notes"],
        )

    def upsert_dm_queue(self, entries: list[DMQueueEntry], run_id: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect(immediate=True) as conn:
            conn.execute("UPDATE dm_queue SET is_current = 0")
            for e in entries:
                active_run = e.run_id or run_id or ""
                valid_ig = has_valid_instagram_target(e.instagram_url)
                status = e.status if valid_ig else DMStatus.SKIPPED_NO_HANDLE
                last_event = e.last_event_status if valid_ig else DMStatus.SKIPPED_NO_HANDLE
                sent_val = 1 if (valid_ig and e.sent) else 0
                sent_date = e.sent_date if sent_val else None
                notes = (
                    e.notes
                    if valid_ig
                    else "No public Instagram handle/URL found; DM skipped (draft copy retained for manual lookup)."
                )
                conn.execute(
                    """
                    INSERT INTO dm_queue (
                        run_id, is_current, influencer_id, influencer_name, instagram_url, instagram_dm,
                        dm_word_count, sent, sent_date, status, last_event_status, duplicate_attempts,
                        notes, updated_at
                    )
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(influencer_id) DO UPDATE SET
                        run_id = CASE
                            WHEN dm_queue.sent = 1 AND dm_queue.run_id != '' AND excluded.status != 'Skipped - No Instagram Handle'
                            THEN dm_queue.run_id
                            ELSE excluded.run_id
                        END,
                        is_current = 1,
                        influencer_name = excluded.influencer_name,
                        instagram_url = excluded.instagram_url,
                        instagram_dm = excluded.instagram_dm,
                        dm_word_count = excluded.dm_word_count,
                        sent = CASE
                            WHEN excluded.status = 'Skipped - No Instagram Handle' THEN 0
                            ELSE dm_queue.sent
                        END,
                        sent_date = CASE
                            WHEN excluded.status = 'Skipped - No Instagram Handle' THEN NULL
                            ELSE dm_queue.sent_date
                        END,
                        status = CASE
                            WHEN excluded.status = 'Skipped - No Instagram Handle' THEN excluded.status
                            WHEN dm_queue.status = 'Skipped - No Instagram Handle' AND excluded.status != 'Skipped - No Instagram Handle' THEN excluded.status
                            ELSE dm_queue.status
                        END,
                        last_event_status = CASE
                            WHEN excluded.status = 'Skipped - No Instagram Handle' THEN excluded.last_event_status
                            WHEN dm_queue.status = 'Skipped - No Instagram Handle' AND excluded.status != 'Skipped - No Instagram Handle' THEN excluded.last_event_status
                            ELSE dm_queue.last_event_status
                        END,
                        notes = CASE
                            WHEN excluded.status = 'Skipped - No Instagram Handle' THEN excluded.notes
                            WHEN dm_queue.status = 'Skipped - No Instagram Handle' AND excluded.status != 'Skipped - No Instagram Handle' THEN excluded.notes
                            ELSE dm_queue.notes
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        active_run,
                        e.influencer_id,
                        e.influencer_name,
                        e.instagram_url,
                        e.instagram_dm,
                        e.dm_word_count,
                        sent_val,
                        sent_date,
                        status.value,
                        last_event.value,
                        e.duplicate_attempts,
                        notes,
                        now,
                    ),
                )
                if not valid_ig:
                    already_logged = conn.execute(
                        """
                        SELECT 1 FROM dm_dispatch_log
                        WHERE run_id = ? AND influencer_id = ? AND status = ?
                        LIMIT 1
                        """,
                        (active_run, e.influencer_id, DMStatus.SKIPPED_NO_HANDLE.value),
                    ).fetchone()
                    if not already_logged:
                        conn.execute(
                            """
                            INSERT INTO dm_dispatch_log (run_id, influencer_id, influencer_name, status, sent, notes, created_at)
                            VALUES (?, ?, ?, ?, 0, ?, ?)
                            """,
                            (
                                active_run,
                                e.influencer_id,
                                e.influencer_name,
                                DMStatus.SKIPPED_NO_HANDLE.value,
                                notes,
                                now,
                            ),
                        )

    def update_dm_status(self, influencer_id: str, status: DMStatus, notes: str = "") -> DMQueueEntry | None:
        now = datetime.now(UTC).isoformat()
        sent_flag = 1 if status in (DMStatus.SIMULATED_SENT, DMStatus.MARKED_SENT_MANUALLY) else 0
        sent_date = now if sent_flag else None
        with self._connect(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM dm_queue WHERE influencer_id = ?", (influencer_id,)
            ).fetchone()
            if not existing:
                return None
            if not has_valid_instagram_target(existing["instagram_url"]):
                skip_note = (
                    "No public Instagram handle/URL found; DM skipped "
                    "(draft copy retained for manual lookup)."
                )
                conn.execute(
                    """
                    UPDATE dm_queue
                    SET sent = 0, sent_date = NULL, status = ?, last_event_status = ?, notes = ?, updated_at = ?
                    WHERE influencer_id = ?
                    """,
                    (
                        DMStatus.SKIPPED_NO_HANDLE.value,
                        DMStatus.SKIPPED_NO_HANDLE.value,
                        skip_note,
                        now,
                        influencer_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO dm_dispatch_log (run_id, influencer_id, influencer_name, status, sent, notes, created_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        existing["run_id"],
                        existing["influencer_id"],
                        existing["influencer_name"],
                        DMStatus.SKIPPED_NO_HANDLE.value,
                        skip_note,
                        now,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM dm_queue WHERE influencer_id = ?", (influencer_id,)
                ).fetchone()
                return self._row_to_dm_entry(updated)
            if existing["sent"] == 1 and sent_flag == 1:
                dup_count = int(existing["duplicate_attempts"] or 0) + 1
                dup_note = f"Duplicate DM prevented (attempt #{dup_count}): already sent previously."
                conn.execute(
                    """
                    UPDATE dm_queue
                    SET last_event_status = ?, duplicate_attempts = ?, notes = ?, updated_at = ?
                    WHERE influencer_id = ?
                    """,
                    (DMStatus.SKIPPED_DUPLICATE.value, dup_count, dup_note, now, influencer_id),
                )
                conn.execute(
                    """
                    INSERT INTO dm_dispatch_log (run_id, influencer_id, influencer_name, status, sent, notes, created_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        existing["run_id"],
                        existing["influencer_id"],
                        existing["influencer_name"],
                        DMStatus.SKIPPED_DUPLICATE.value,
                        dup_note,
                        now,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM dm_queue WHERE influencer_id = ?", (influencer_id,)
                ).fetchone()
                return DMQueueEntry(
                    id=updated["id"],
                    run_id=updated["run_id"],
                    influencer_id=updated["influencer_id"],
                    influencer_name=updated["influencer_name"],
                    instagram_url=updated["instagram_url"],
                    instagram_dm=updated["instagram_dm"],
                    dm_word_count=updated["dm_word_count"],
                    sent=bool(updated["sent"]),
                    sent_date=updated["sent_date"],
                    status=DMStatus.SKIPPED_DUPLICATE,
                    last_event_status=DMStatus(updated["last_event_status"]),
                    duplicate_attempts=int(updated["duplicate_attempts"] or 0),
                    notes=updated["notes"],
                )

            conn.execute(
                """
                UPDATE dm_queue
                SET sent = ?, sent_date = ?, status = ?, last_event_status = ?, notes = ?, updated_at = ?
                WHERE influencer_id = ?
                """,
                (sent_flag, sent_date, status.value, status.value, notes, now, influencer_id),
            )
            conn.execute(
                """
                INSERT INTO dm_dispatch_log (run_id, influencer_id, influencer_name, status, sent, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    existing["run_id"],
                    existing["influencer_id"],
                    existing["influencer_name"],
                    status.value,
                    sent_flag,
                    notes,
                    now,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM dm_queue WHERE influencer_id = ?", (influencer_id,)
            ).fetchone()
        return self._row_to_dm_entry(updated)

    def get_dm_queue(self, current_only: bool = True) -> list[DMQueueEntry]:
        with self._connect() as conn:
            if current_only:
                rows = conn.execute(
                    """
                    SELECT dm.* FROM dm_queue dm
                    JOIN influencers i ON dm.influencer_id = i.id
                    WHERE dm.is_current = 1 AND i.is_current = 1 AND i.status = 'Qualified'
                    ORDER BY dm.id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM dm_queue ORDER BY id ASC").fetchall()
        return [self._row_to_dm_entry(r) for r in rows]

    def get_dm_dispatch_log(self, influencer_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if influencer_id:
                rows = conn.execute(
                    "SELECT * FROM dm_dispatch_log WHERE influencer_id = ? ORDER BY id DESC",
                    (influencer_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM dm_dispatch_log ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_dm_entry(r: sqlite3.Row) -> DMQueueEntry:
        return DMQueueEntry(
            id=r["id"],
            run_id=r["run_id"],
            influencer_id=r["influencer_id"],
            influencer_name=r["influencer_name"],
            instagram_url=r["instagram_url"],
            instagram_dm=r["instagram_dm"],
            dm_word_count=r["dm_word_count"],
            sent=bool(r["sent"]),
            sent_date=r["sent_date"],
            status=DMStatus(r["status"]),
            last_event_status=DMStatus(r["last_event_status"]),
            duplicate_attempts=int(r["duplicate_attempts"] or 0),
            notes=r["notes"],
        )

    def save_pipeline_run(self, summary: PipelineSummary) -> None:
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pipeline_runs (run_id, niche, summary_json, executed_at)
                VALUES (?, ?, ?, ?)
                """,
                (summary.run_id, summary.niche, summary.model_dump_json(), summary.executed_at),
            )

    def get_latest_run(self) -> PipelineSummary | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary_json FROM pipeline_runs ORDER BY executed_at DESC LIMIT 1"
            ).fetchone()
        return PipelineSummary.model_validate_json(row["summary_json"]) if row else None
