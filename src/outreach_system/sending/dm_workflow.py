"""Compliant simulated/manual Instagram DM workflow without bypassing platform restrictions."""

from __future__ import annotations

from outreach_system.models import (
    DMQueueEntry,
    DMStatus,
    PersonalizedMessage,
    has_valid_instagram_target,
)
from outreach_system.storage.database import OutreachDatabase


class InstagramDMWorkflow:
    """Manages the review, copy-ready manual queue, and simulated sending of Instagram DMs."""

    def __init__(self, db: OutreachDatabase) -> None:
        self.db = db

    def sync_queue(
        self,
        messages: list[PersonalizedMessage],
        run_id: str = "",
    ) -> list[DMQueueEntry]:
        """Register current-run personalized DMs into the review/manual-send queue."""
        existing = {
            entry.influencer_id: entry for entry in self.db.get_dm_queue(current_only=False)
        }
        to_upsert: list[DMQueueEntry] = []

        for msg in messages:
            active_run = run_id or msg.run_id
            valid_ig = has_valid_instagram_target(msg.instagram_url)
            if msg.influencer_id in existing:
                cur = existing[msg.influencer_id]
                cur.run_id = active_run
                cur.instagram_url = msg.instagram_url
                cur.instagram_dm = msg.instagram_dm
                cur.dm_word_count = msg.dm_word_count
                if not valid_ig:
                    cur.sent = False
                    cur.sent_date = None
                    cur.status = DMStatus.SKIPPED_NO_HANDLE
                    cur.last_event_status = DMStatus.SKIPPED_NO_HANDLE
                    cur.notes = (
                        "No public Instagram handle/URL found; DM skipped "
                        "(draft copy retained for manual lookup)."
                    )
                elif cur.status == DMStatus.SKIPPED_NO_HANDLE:
                    cur.status = DMStatus.PENDING_MANUAL
                    cur.last_event_status = DMStatus.PENDING_MANUAL
                    cur.notes = "Ready for manual Instagram DM send (copy message & open IG profile)."
                to_upsert.append(cur)
            else:
                if valid_ig:
                    status = DMStatus.PENDING_MANUAL
                    notes = "Ready for manual Instagram DM send (copy message & open IG profile)."
                else:
                    status = DMStatus.SKIPPED_NO_HANDLE
                    notes = (
                        "No public Instagram handle/URL found; DM skipped "
                        "(draft copy retained for manual lookup)."
                    )
                to_upsert.append(
                    DMQueueEntry(
                        run_id=active_run,
                        influencer_id=msg.influencer_id,
                        influencer_name=msg.influencer_name,
                        instagram_url=msg.instagram_url,
                        instagram_dm=msg.instagram_dm,
                        dm_word_count=msg.dm_word_count,
                        sent=False,
                        status=status,
                        last_event_status=status,
                        duplicate_attempts=0,
                        notes=notes,
                    )
                )
        self.db.upsert_dm_queue(to_upsert, run_id=active_run if messages else run_id)
        return self.db.get_dm_queue(current_only=True)

    def simulate_send(self, influencer_id: str) -> DMQueueEntry | None:
        """Simulate dispatching an Instagram DM safely without hitting private Instagram endpoints."""
        return self.db.update_dm_status(
            influencer_id,
            DMStatus.SIMULATED_SENT,
            notes="Simulated Instagram DM dispatch recorded (compliant manual/simulation workflow).",
        )

    def mark_sent_manually(self, influencer_id: str) -> DMQueueEntry | None:
        """Record that a human operator sent the personalized DM directly on Instagram."""
        return self.db.update_dm_status(
            influencer_id,
            DMStatus.MARKED_SENT_MANUALLY,
            notes="Operator confirmed manual Instagram DM delivery.",
        )
