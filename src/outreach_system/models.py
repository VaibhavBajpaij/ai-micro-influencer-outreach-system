"""Pydantic data models for every stage of the micro-influencer pipeline."""

from enum import StrEnum

from pydantic import BaseModel, Field


class FilterStatus(StrEnum):
    QUALIFIED = "Qualified"
    REJECTED = "Rejected"


class EmailSendStatus(StrEnum):
    PENDING = "Pending Review"
    SENDING = "Sending"
    SENT_SIMULATED = "Sent (Simulated)"
    SENT_SMTP = "Sent (SMTP)"
    SKIPPED_NO_EMAIL = "Skipped - No Email"
    SKIPPED_DUPLICATE = "Skipped - Duplicate"
    MANUAL_VERIFICATION_REQUIRED = "Verify Delivery - Uncertain SMTP"
    SKIPPED_VERIFICATION_PENDING = "Skipped - Verification Pending"
    FAILED = "Failed"


class DMStatus(StrEnum):
    PENDING_MANUAL = "Pending Manual Send"
    SIMULATED_SENT = "Simulated Sent"
    MARKED_SENT_MANUALLY = "Marked Sent Manually"
    SKIPPED_DUPLICATE = "Skipped - Duplicate"
    SKIPPED_NO_HANDLE = "Skipped - No Instagram Handle"


def has_valid_instagram_target(instagram_url: str | None) -> bool:
    """Return True only when an explicit, non-placeholder Instagram URL or @handle exists."""
    if not instagram_url:
        return False
    cleaned = instagram_url.strip()
    lower = cleaned.lower()
    if lower in ("", "not found", "none", "n/a", "null", "-"):
        return False
    if lower.startswith("@"):
        return len(lower) > 1
    if "instagram.com/" in lower:
        slug = lower.split("instagram.com/", 1)[1].strip("/")
        return bool(slug) and slug not in ("none", "null", "not-found")
    return False


class ContentItem(BaseModel):
    """A real recent post or video fetched from the creator's public feed."""

    title: str
    url: str
    published_at: str | None = None
    views: int | None = None
    likes: int | None = None
    comments_or_replies: int | None = None
    reposts: int | None = None
    description_snippet: str = ""


class InfluencerRaw(BaseModel):
    """Raw discovered influencer record before filtering and deep enrichment."""

    id: str
    name: str
    handle: str = ""
    platform: str
    profile_url: str
    followers: int | None = None
    avg_views: float | None = None
    avg_likes: float | None = None
    avg_interactions: float | None = None
    engagement_rate: float | None = None
    engagement_by_views_pct: float | None = None
    view_to_subscriber_pct: float | None = None
    engagement_formula: str = ""
    bio: str = ""
    country: str | None = None
    discovery_query: str = ""
    external_links: list[str] = Field(default_factory=list)
    recent_content: list[ContentItem] = Field(default_factory=list)
    last_post_date: str | None = None
    raw_video_descriptions: list[str] = Field(default_factory=list)


class FilterDecision(BaseModel):
    """Transparent classification and pass/fail audit for one influencer."""

    status: FilterStatus
    tier: str
    filter_score: float = Field(ge=0.0, le=100.0)
    niche_relevance_score: float = Field(ge=0.0, le=1.0)
    brand_fit_score: float = Field(ge=0.0, le=100.0)
    passed_criteria: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    filter_summary: str


class InfluencerProfile(BaseModel):
    """Unified influencer record combining discovery, filtering, and enrichment."""

    # Mandatory Assignment Fields (Section 3)
    id: str
    name: str
    handle: str = ""
    platform: str
    profile_url: str
    followers: int | None = None
    engagement_rate: float | None = None
    niche: str
    content_themes: list[str] = Field(default_factory=list)
    contact_email: str = "Not Found"

    # Optional Assignment Fields (Section 3)
    instagram_url: str = "Not Found"
    youtube_url: str = "Not Found"
    tiktok_url: str = "Not Found"
    website: str = "Not Found"
    audience_age: str = "Not Available (Private Analytics)"
    audience_gender: str = "Not Available (Private Analytics)"
    audience_geography: str = "Not Available"

    # Extended Signals & Metrics
    bio: str = ""
    avg_views: float | None = None
    avg_likes: float | None = None
    engagement_by_views_pct: float | None = None
    view_to_subscriber_pct: float | None = None
    engagement_formula: str = ""
    content_tone_style: str = ""
    target_audience_segment: str = ""
    email_source: str = "none"
    last_post_date: str | None = None
    discovery_query: str = ""
    recent_content: list[ContentItem] = Field(default_factory=list)

    # Filtering & Classification Audit
    run_id: str = ""
    status: FilterStatus = FilterStatus.REJECTED
    outreach_ready: bool = False
    outreach_readiness: str = "Rejected"
    tier: str = ""
    filter_score: float = 0.0
    niche_relevance_score: float = 0.0
    brand_fit_score: float = 0.0
    passed_criteria: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    filter_summary: str = ""
    identity_cluster_id: str = ""
    cross_platform_note: str = ""


class PersonalizedMessage(BaseModel):
    """Personalized Email (60-90 words) and Instagram DM (15-30 words) for a shortlisted creator."""

    run_id: str = ""
    influencer_id: str
    influencer_name: str
    platform: str
    contact_email: str
    instagram_url: str = "Not Found"
    collaboration_angle: str
    email_subject: str
    email_pitch: str
    email_word_count: int
    instagram_dm: str
    dm_word_count: int
    personalization_signals_used: list[str] = Field(default_factory=list)
    generation_mode: str = "contextual_fallback"
    llm_provider: str
    llm_model: str
    generated_at: str


class OutreachLogEntry(BaseModel):
    """Record in the Outreach Tracker for email delivery and deduplication."""

    id: int | None = None
    run_id: str = ""
    influencer_id: str
    influencer_name: str
    email: str
    message_generated: bool
    email_subject: str = ""
    sent: bool
    sent_date: str | None = None
    status: EmailSendStatus
    send_mode: str = "simulate"
    receipt_id: str = ""
    notes: str = ""


class DMQueueEntry(BaseModel):
    """Simulated / manual sending workflow entry for Instagram DMs."""

    id: int | None = None
    run_id: str = ""
    influencer_id: str
    influencer_name: str
    instagram_url: str
    instagram_dm: str
    dm_word_count: int
    sent: bool = False
    sent_date: str | None = None
    status: DMStatus = DMStatus.PENDING_MANUAL
    last_event_status: DMStatus = DMStatus.PENDING_MANUAL
    duplicate_attempts: int = 0
    notes: str = ""


class PipelineSummary(BaseModel):
    """Summary metrics returned after executing a full or partial pipeline run."""

    run_id: str
    niche: str
    discovered_count: int
    qualified_count: int
    outreach_ready_count: int = 0
    qualified_missing_email_count: int = 0
    rejected_count: int
    emails_found_count: int
    messages_generated_count: int
    emails_sent_count: int
    emails_skipped_no_email: int
    emails_skipped_duplicate: int
    dm_queued_count: int
    dm_sent_count: int = 0
    dm_skipped_no_handle: int = 0
    generation_mode: str = "contextual_fallback"
    llm_provider_used: str
    llm_model_used: str
    executed_at: str


def resolve_generation_mode(provider: str) -> str:
    """Distinguish genuine LLM provider outputs from contextual_fallback deterministic generation."""
    normalized = (provider or "").strip().lower()
    if normalized in {"gemini", "openai", "groq", "anthropic", "ollama"}:
        return "LLM"
    return "contextual_fallback"
