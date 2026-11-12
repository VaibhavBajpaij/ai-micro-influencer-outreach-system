"""Tests for profile enrichment, single-char emails, and strict anti-fabrication OSINT."""

from datetime import UTC, datetime, timedelta

import pytest

from outreach_system.config import Settings, get_niche_preset
from outreach_system.enrichment import ProfileEnricher, extract_emails_from_text
from outreach_system.filtering import InfluencerClassifier
from outreach_system.models import ContentItem, FilterStatus, InfluencerRaw


def _recent() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).isoformat()


def test_extract_emails_ignores_placeholders_and_supports_single_char_local_parts() -> None:
    sample = (
        "Contact noreply@company.com or privacy@youtube.com or 2x@icon.png. "
        "Business:-sonutechindia70@gmail.com | a@devstudio.io | collabs@techdesk.io and "
        "backup: alex [at] buildlab [dot] dev"
    )
    emails = extract_emails_from_text(sample)
    assert emails == [
        "sonutechindia70@gmail.com",
        "a@devstudio.io",
        "collabs@techdesk.io",
        "alex@buildlab.dev",
    ]


@pytest.mark.asyncio
async def test_enricher_never_guesses_missing_email_and_separates_readiness() -> None:
    preset = get_niche_preset("technology")
    clf = InfluencerClassifier(Settings(), preset)
    raw = InfluencerRaw(
        id="yt_noemail",
        name="Silent Hardware",
        platform="YouTube",
        profile_url="https://www.youtube.com/@silenthardware",
        followers=14_000,
        avg_views=4_200.0,
        engagement_rate=1.2,
        bio="Honest laptop, mechanical keyboard, and gadget teardowns. IG: @silenthardware_ig",
        external_links=["https://instagram.com/silenthardware_ig"],
        recent_content=[
            ContentItem(
                title="MacBook Pro M4 Thermal Mod & SSD Review",
                url="https://youtube.com/watch?v=1",
                published_at=_recent(),
            )
        ],
        last_post_date=_recent(),
    )
    decision = clf.evaluate(raw)
    assert decision.status == FilterStatus.QUALIFIED
    enricher = ProfileEnricher(preset)
    prof = (await enricher.enrich_all([(raw, decision)], crawl_websites_for_rejected=False))[0]
    assert prof.contact_email == "Not Found"
    assert prof.outreach_ready is False
    assert prof.outreach_readiness == "Qualified (Not Email-Ready - DM Only)"
    assert prof.instagram_url == "https://instagram.com/silenthardware_ig"
    assert prof.audience_age == "Not Available (Private Analytics)"
    assert prof.audience_gender == "Not Available (Private Analytics)"


@pytest.mark.asyncio
async def test_enricher_extracts_email_from_video_description_and_marks_outreach_ready() -> None:
    preset = get_niche_preset("technology")
    clf = InfluencerClassifier(Settings(), preset)
    raw = InfluencerRaw(
        id="yt_videmail",
        name="Dev Desk Pro",
        platform="YouTube",
        profile_url="https://www.youtube.com/@devdeskpro",
        followers=32_000,
        avg_views=8_000.0,
        engagement_rate=1.8,
        bio="Developer workspace setups, ergonomic keyboards, & AI coding tools.",
        raw_video_descriptions=["Sponsors & collabs: partnerships@devdeskpro.com\nGear links below!"],
        recent_content=[
            ContentItem(
                title="My 2026 Cursor AI + Desk Setup Tour",
                url="https://youtube.com/watch?v=2",
                published_at=_recent(),
            )
        ],
        last_post_date=_recent(),
    )
    decision = clf.evaluate(raw)
    assert decision.status == FilterStatus.QUALIFIED
    enricher = ProfileEnricher(preset)
    prof = (await enricher.enrich_all([(raw, decision)]))[0]
    assert prof.contact_email == "partnerships@devdeskpro.com"
    assert prof.email_source == "video_description"
    assert prof.outreach_ready is True
    assert prof.outreach_readiness == "Outreach-Ready (Email Verified)"
    assert "AI & Automation Tools" in prof.content_themes or "Desk Setup & Peripherals" in prof.content_themes
