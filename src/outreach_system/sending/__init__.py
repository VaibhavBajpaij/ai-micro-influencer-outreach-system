"""Outreach sending layer for Email (simulated/SMTP) and compliant Instagram DM queues."""

from outreach_system.sending.dm_workflow import InstagramDMWorkflow
from outreach_system.sending.email_sender import (
    SUPPORTED_SEND_MODES,
    EmailOutreachSender,
    is_sendable_email,
    validate_send_mode,
)

__all__ = [
    "SUPPORTED_SEND_MODES",
    "EmailOutreachSender",
    "InstagramDMWorkflow",
    "is_sendable_email",
    "validate_send_mode",
]

