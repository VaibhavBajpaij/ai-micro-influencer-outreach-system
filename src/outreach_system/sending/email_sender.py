"""Email outreach dispatcher supporting safe simulation outbox and live SMTP SSL/TLS delivery."""

from __future__ import annotations

import json
import re
import smtplib
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from outreach_system.config import Settings
from outreach_system.models import (
    EmailSendStatus,
    InfluencerProfile,
    OutreachLogEntry,
    PersonalizedMessage,
)
from outreach_system.storage.database import OutreachDatabase

SUPPORTED_SEND_MODES = ("simulate", "smtp")
VALID_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._%+\-]*[A-Za-z0-9])?@[A-Za-z0-9.\-]+\.[A-Za-z]{2,15}$"
)


def is_sendable_email(email: str | None) -> bool:
    """Verify email exists and is syntactically valid (supports single-char local parts; excludes 'Not Found')."""
    if not email:
        return False
    cleaned = email.strip()
    if cleaned.lower() in ("not found", "none", "n/a", ""):
        return False
    return bool(VALID_EMAIL_RE.match(cleaned))


def validate_send_mode(mode: str | None, default_mode: str = "simulate") -> str:
    """Strictly validate sending mode against supported options ('simulate' | 'smtp')."""
    resolved = (mode if mode is not None else default_mode).strip().lower()
    if resolved not in SUPPORTED_SEND_MODES:
        raise ValueError(
            f"Unsupported sending mode '{mode}'. Supported modes: {', '.join(SUPPORTED_SEND_MODES)}"
        )
    return resolved


PRE_SEND_SMTP_EXCEPTIONS = (
    RuntimeError,
    ValueError,
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPRecipientsRefused,
)


class EmailOutreachSender:
    """Executes personalized email outreach with atomic SQLite deduplication and audit logging."""

    def __init__(self, settings: Settings, db: OutreachDatabase) -> None:
        self.settings = settings
        self.db = db
        self.outbox_dir = self.settings.resolve_outputs_dir() / "outbox"
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def dispatch_all(
        self,
        shortlisted: list[InfluencerProfile],
        mode_override: str | None = None,
        influencer_ids: set[str] | None = None,
        run_id: str = "",
    ) -> list[OutreachLogEntry]:
        """Process selected shortlisted influencers: validate email, claim atomic slot, send/simulate, log."""
        mode = validate_send_mode(mode_override, self.settings.send_mode)
        if mode == "smtp" and not self.settings.allow_live_smtp:
            raise PermissionError(
                "Live SMTP sending is disabled (set ALLOW_LIVE_SMTP=true to authorize live delivery)."
            )

        self.db.reconcile_stale_sending_slots(
            stale_after_seconds=self.settings.stale_sending_timeout_seconds
        )

        results: list[OutreachLogEntry] = []
        for profile in shortlisted:
            if influencer_ids is not None and profile.id not in influencer_ids:
                continue
            entry = self.dispatch_single(
                profile,
                mode=mode,
                run_id=run_id or profile.run_id,
                reconcile_stale=False,
            )
            results.append(entry)
        return results

    def dispatch_single(
        self,
        profile: InfluencerProfile,
        mode: str | None = None,
        run_id: str = "",
        *,
        reconcile_stale: bool = True,
    ) -> OutreachLogEntry:
        """Process outreach for a single influencer with atomic duplicate locking."""
        resolved_mode = validate_send_mode(mode, self.settings.send_mode)
        if resolved_mode == "smtp" and not self.settings.allow_live_smtp:
            raise PermissionError(
                "Live SMTP sending is disabled (set ALLOW_LIVE_SMTP=true to authorize live delivery)."
            )

        if reconcile_stale:
            self.db.reconcile_stale_sending_slots(
                stale_after_seconds=self.settings.stale_sending_timeout_seconds
            )
        active_run_id = run_id or profile.run_id
        msg = self.db.get_message_for_influencer(profile.id)
        has_msg = msg is not None

        # 1. Check valid contact email
        if not is_sendable_email(profile.contact_email):
            entry = OutreachLogEntry(
                run_id=active_run_id,
                influencer_id=profile.id,
                influencer_name=profile.name,
                email=profile.contact_email or "Not Found",
                message_generated=has_msg,
                email_subject=msg.email_subject if msg else "",
                sent=False,
                sent_date=None,
                status=EmailSendStatus.SKIPPED_NO_EMAIL,
                send_mode=resolved_mode,
                notes="No public contact email found; queued for Instagram DM / manual outreach.",
            )
            entry.id = self.db.append_outreach_log(entry)
            return entry

        # 2. Ensure personalized message exists
        if msg is None:
            entry = OutreachLogEntry(
                run_id=active_run_id,
                influencer_id=profile.id,
                influencer_name=profile.name,
                email=profile.contact_email,
                message_generated=False,
                email_subject="",
                sent=False,
                sent_date=None,
                status=EmailSendStatus.PENDING,
                send_mode=resolved_mode,
                notes="Skipped sending: personalized message not yet generated.",
            )
            entry.id = self.db.append_outreach_log(entry)
            return entry

        # 3. Atomically claim unique send slot inside SQLite BEGIN IMMEDIATE transaction
        receipt_id = (
            f"sim-{uuid.uuid4().hex[:12]}"
            if resolved_mode == "simulate"
            else f"smtp-{uuid.uuid4().hex[:12]}"
        )
        claimed, log_id, dup_entry = self.db.claim_outreach_slot(
            run_id=active_run_id or msg.run_id,
            influencer_id=profile.id,
            influencer_name=profile.name,
            email=profile.contact_email,
            email_subject=msg.email_subject,
            send_mode=resolved_mode,
            receipt_id=receipt_id,
        )
        if not claimed and dup_entry is not None:
            return dup_entry

        # 4. Build RFC-2822 EmailMessage and deliver via simulation or SMTP
        now = datetime.now(UTC).isoformat()
        try:
            email_msg = self._build_mime_message(profile.contact_email, msg)
        except Exception as exc:
            return self.db.finalize_outreach_slot(
                log_id,
                status=EmailSendStatus.FAILED,
                sent=False,
                sent_date=None,
                receipt_id="",
                notes=f"MIME message generation failed: {exc}",
            )

        if resolved_mode == "smtp":
            try:
                self._send_via_smtp(email_msg)
                return self.db.finalize_outreach_slot(
                    log_id,
                    status=EmailSendStatus.SENT_SMTP,
                    sent=True,
                    sent_date=now,
                    receipt_id=receipt_id,
                    notes=f"Delivered via SMTP ({self.settings.smtp_host}:{self.settings.smtp_port}).",
                )
            except PRE_SEND_SMTP_EXCEPTIONS as exc:
                return self.db.finalize_outreach_slot(
                    log_id,
                    status=EmailSendStatus.FAILED,
                    sent=False,
                    sent_date=None,
                    receipt_id="",
                    notes=f"SMTP pre-send failure (confirmed not transmitted): {exc}",
                )
            except Exception as exc:
                return self.db.finalize_outreach_slot(
                    log_id,
                    status=EmailSendStatus.MANUAL_VERIFICATION_REQUIRED,
                    sent=False,
                    sent_date=None,
                    receipt_id=receipt_id,
                    notes=(
                        f"Uncertain SMTP transmission outcome ({type(exc).__name__}: {exc}). "
                        f"Verify recipient inbox / mailserver logs before confirming delivery or unlocking retry."
                    ),
                )

        try:
            eml_path = self._write_simulated_outbox(profile.id, email_msg, msg, receipt_id, now)
            return self.db.finalize_outreach_slot(
                log_id,
                status=EmailSendStatus.SENT_SIMULATED,
                sent=True,
                sent_date=now,
                receipt_id=receipt_id,
                notes=f"Simulated dispatch archived to outputs/outbox/{eml_path.name}",
            )
        except Exception as exc:
            self._cleanup_failed_outbox(profile.id, receipt_id)
            return self.db.finalize_outreach_slot(
                log_id,
                status=EmailSendStatus.FAILED,
                sent=False,
                sent_date=None,
                receipt_id="",
                notes=f"Simulated outbox write failed: {exc}",
            )

    def _cleanup_failed_outbox(self, influencer_id: str, receipt_id: str) -> None:
        for suffix in (".eml", ".receipt.json"):
            candidate = self.outbox_dir / f"{influencer_id}_{receipt_id}{suffix}"
            if candidate.exists():
                try:
                    candidate.unlink()
                except OSError:
                    pass

    def _build_mime_message(self, to_email: str, msg: PersonalizedMessage) -> EmailMessage:
        mime = EmailMessage()
        reply_to = self.settings.reply_to_email or self.settings.sender_email
        mime["From"] = f"{self.settings.sender_name} <{self.settings.sender_email}>"
        mime["To"] = to_email
        mime["Reply-To"] = reply_to
        mime["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=Unsubscribe>"
        mime["Subject"] = msg.email_subject
        mime.set_content(msg.email_pitch)
        return mime

    def _write_simulated_outbox(
        self,
        influencer_id: str,
        mime: EmailMessage,
        msg: PersonalizedMessage,
        receipt_id: str,
        timestamp: str,
    ) -> Path:
        eml_path = self.outbox_dir / f"{influencer_id}_{receipt_id}.eml"
        eml_path.write_bytes(mime.as_bytes())

        receipt_path = self.outbox_dir / f"{influencer_id}_{receipt_id}.receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "receipt_id": receipt_id,
                    "run_id": msg.run_id,
                    "influencer_id": influencer_id,
                    "influencer_name": msg.influencer_name,
                    "to": msg.contact_email,
                    "from": mime["From"],
                    "reply_to": mime["Reply-To"],
                    "list_unsubscribe": mime["List-Unsubscribe"],
                    "subject": msg.email_subject,
                    "word_count": msg.email_word_count,
                    "collaboration_angle": msg.collaboration_angle,
                    "dispatched_at": timestamp,
                    "mode": "simulated",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return eml_path

    def _send_via_smtp(self, mime: EmailMessage) -> None:
        if not self.settings.smtp_user or not self.settings.smtp_password:
            raise RuntimeError("SMTP_USER and SMTP_PASSWORD must be configured for live smtp mode")
        use_ssl = self.settings.smtp_use_ssl or self.settings.smtp_port == 465
        if use_ssl:
            with smtplib.SMTP_SSL(
                self.settings.smtp_host, self.settings.smtp_port, timeout=20
            ) as server:
                server.login(self.settings.smtp_user, self.settings.smtp_password)
                server.send_message(mime)
        else:
            with smtplib.SMTP(
                self.settings.smtp_host, self.settings.smtp_port, timeout=20
            ) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.settings.smtp_user, self.settings.smtp_password)
                server.send_message(mime)
