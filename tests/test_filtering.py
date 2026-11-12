"""Tests for micro-influencer filtering, classification, and pass/fail audit reasons."""

from datetime import UTC, datetime, timedelta

from outreach_system.config import Settings, get_niche_preset
from outreach_system.enrichment import annotate_cross_platform_identities
from outreach_system.filtering import InfluencerClassifier
from outreach_system.models import ContentItem, FilterStatus, InfluencerProfile, InfluencerRaw


def _recent_iso(days_ago: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _make_raw(**overrides) -> InfluencerRaw:
    base = dict(
        id="yt_test1",
        name="Tech Builder Lab",
        handle="@techbuilderlab",
        platform="YouTube",
        profile_url="https://www.youtube.com/@techbuilderlab",
        followers=24_500,
        avg_views=5_200.0,
        avg_likes=310.0,
        engagement_rate=1.27,
        engagement_by_views_pct=5.96,
        view_to_subscriber_pct=21.2,
        bio="Mechanical keyboard reviews, AI coding workflows, and minimalist desk setup guides.",
        country="United States",
        discovery_query="mechanical keyboard review",
        recent_content=[
            ContentItem(
                title="Best Split Mechanical Keyboard Review for Developers",
                url="https://www.youtube.com/watch?v=abc",
                published_at=_recent_iso(4),
                views=5200,
                likes=310,
            )
        ],
        last_post_date=_recent_iso(4),
    )
    base.update(overrides)
    return InfluencerRaw(**base)


def test_qualified_micro_influencer_passes() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    decision = clf.evaluate(_make_raw())
    assert decision.status == FilterStatus.QUALIFIED
    assert decision.failed_criteria == []
    assert decision.filter_score >= 60.0
    assert "PASS" in decision.filter_summary


def test_high_engagement_nano_exception_passes() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    nano = _make_raw(
        id="yt_nano_hi", followers=3_200, avg_views=4_500.0, avg_likes=120.0, engagement_rate=3.75
    )
    decision = clf.evaluate(nano)
    assert decision.status == FilterStatus.QUALIFIED
    assert "High-Engagement Nano" in decision.tier


def test_macro_influencer_is_rejected_with_clear_reason() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    macro = _make_raw(id="yt_macro", followers=450_000, engagement_rate=1.8)
    decision = clf.evaluate(macro)
    assert decision.status == FilterStatus.REJECTED
    assert "Macro" in decision.tier
    assert any("exceed micro-influencer ceiling" in r for r in decision.failed_criteria)


def test_inactive_and_brand_safety_rejected() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    bad = _make_raw(
        id="yt_bad",
        bio="Tech gadgets and online casino betting affiliate links",
        last_post_date=_recent_iso(240),
    )
    decision = clf.evaluate(bad)
    assert decision.status == FilterStatus.REJECTED
    assert any("Inactive creator" in r for r in decision.failed_criteria)
    assert any("Brand safety exclusion" in r for r in decision.failed_criteria)


def test_off_niche_rejected() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    off = _make_raw(
        id="yt_cook",
        name="Daily Pasta Recipes",
        bio="Italian cooking videos, homemade sourdough bread, and kitchen pantry tours.",
        discovery_query="",
        recent_content=[
            ContentItem(
                title="How to make fresh pasta dough from scratch",
                url="https://www.youtube.com/watch?v=pasta",
                published_at=_recent_iso(3),
            )
        ],
    )
    decision = clf.evaluate(off)
    assert decision.status == FilterStatus.REJECTED
    assert any("Content niche relevance" in r for r in decision.failed_criteria)


def test_missing_recency_is_rejected_by_strict_policy() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    undated = _make_raw(id="yt_undated", last_post_date=None)
    decision = clf.evaluate(undated)
    assert decision.status == FilterStatus.REJECTED
    assert decision.tier == "Rejected (Unverified Activity)"
    assert any("Unknown/Unverified" in r for r in decision.failed_criteria)


def test_name_alone_cannot_pass_niche_relevance() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    misleading_name = _make_raw(
        id="yt_name_only",
        name="AI Tech Gadgets Review Channel",
        bio="Homemade sourdough baking, garden flowers, and daily cooking logs.",
        recent_content=[
            ContentItem(
                title="Morning bread baking with fresh herbs",
                url="https://www.youtube.com/watch?v=bread",
                published_at=_recent_iso(2),
                views=4_000,
                likes=120,
            )
        ],
        raw_video_descriptions=["Baked sourdough loaves in our kitchen oven."],
    )
    decision = clf.evaluate(misleading_name)
    assert decision.status == FilterStatus.REJECTED
    assert decision.niche_relevance_score == 0.0
    assert any("Content niche relevance" in r for r in decision.failed_criteria)


def test_tightened_engagement_rejects_low_view_loophole_but_allows_high_reach_exception() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))

    # Weak 0.27% engagement with low avg views (955) must NOT pass via old loophole
    weak = _make_raw(
        id="yt_weak",
        followers=12_900,
        avg_views=955.0,
        avg_likes=35.0,
        engagement_rate=0.27,
        engagement_by_views_pct=3.66,
        view_to_subscriber_pct=7.4,
    )
    assert clf.evaluate(weak).status == FilterStatus.REJECTED

    # Tiny views (15 avg views) on 15k channel with artificially computed rate must also fail
    tiny_views = _make_raw(
        id="yt_tiny_views",
        followers=15_000,
        avg_views=15.0,
        avg_likes=70.0,
        engagement_rate=0.47,
    )
    assert clf.evaluate(tiny_views).status == FilterStatus.REJECTED

    # High-reach YouTube exception (>= 0.30%, >= 5k avg views, >= 35% view/sub ratio) passes cleanly
    high_reach = _make_raw(
        id="yt_high_reach",
        followers=20_000,
        avg_views=9_000.0,
        avg_likes=66.0,
        engagement_rate=0.33,
        engagement_by_views_pct=0.73,
        view_to_subscriber_pct=45.0,
    )
    assert clf.evaluate(high_reach).status == FilterStatus.QUALIFIED


def test_conservative_cross_platform_identity_never_merges_name_only_matches() -> None:
    yt_only_name = InfluencerProfile(
        id="yt_alex1",
        name="Alex Chen",
        platform="YouTube",
        profile_url="https://www.youtube.com/@alexchen1",
        niche="Technology & AI Gadgets",
        contact_email="Not Found",
    )
    bsky_only_name = InfluencerProfile(
        id="bsky_alex.bsky.social",
        name="Alex Chen",
        platform="Bluesky",
        profile_url="https://bsky.app/profile/alex.bsky.social",
        niche="Technology & AI Gadgets",
        contact_email="Not Found",
    )
    yt_verified = InfluencerProfile(
        id="yt_jordan",
        name="Jordan Tech",
        platform="YouTube",
        profile_url="https://www.youtube.com/@jordantech",
        niche="Technology & AI Gadgets",
        contact_email="jordan@techcraft.dev",
    )
    bsky_verified = InfluencerProfile(
        id="bsky_jordan.bsky.social",
        name="Jordan",
        platform="Bluesky",
        profile_url="https://bsky.app/profile/jordan.bsky.social",
        niche="Technology & AI Gadgets",
        contact_email="jordan@techcraft.dev",
    )

    annotated = annotate_cross_platform_identities(
        [yt_only_name, bsky_only_name, yt_verified, bsky_verified]
    )
    assert len(annotated) == 4  # platform-specific records preserved
    assert annotated[0].identity_cluster_id != annotated[1].identity_cluster_id
    assert "Uncertain display-name similarity" in annotated[0].cross_platform_note
    assert annotated[2].identity_cluster_id == annotated[3].identity_cluster_id
    assert "Verified cross-platform identity" in annotated[2].cross_platform_note


def test_inactive_only_high_engagement_nano_gets_inactive_tier_not_nano() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))
    inactive_high_eng_nano = _make_raw(
        id="yt_ndr_like",
        followers=2_760,
        avg_views=5_423.0,
        avg_likes=76.5,
        engagement_rate=2.77,
        last_post_date=_recent_iso(352),
    )
    decision = clf.evaluate(inactive_high_eng_nano)
    assert decision.status == FilterStatus.REJECTED
    assert decision.tier == "Rejected (Inactive)"
    assert any("Inactive creator" in r for r in decision.failed_criteria)


def test_distinguishes_unavailable_engagement_from_actual_zero_engagement() -> None:
    clf = InfluencerClassifier(Settings(), get_niche_preset("technology"))

    hidden_likes = _make_raw(
        id="yt_hidden_likes",
        followers=26_200,
        avg_views=48_879.6,
        avg_likes=None,
        engagement_rate=None,
        engagement_by_views_pct=None,
        recent_content=[
            ContentItem(
                title="AI Coding Workflow",
                url="https://www.youtube.com/watch?v=hid1",
                published_at=_recent_iso(5),
                views=48879,
                likes=None,
            )
        ],
    )
    d_hidden = clf.evaluate(hidden_likes)
    assert d_hidden.status == FilterStatus.REJECTED
    assert d_hidden.tier == "Rejected (Engagement Unavailable)"
    assert any("Engagement data unavailable" in r for r in d_hidden.failed_criteria)
    assert not any("below minimum" in r for r in d_hidden.failed_criteria)

    genuine_zero = _make_raw(
        id="yt_zero_likes",
        followers=55_300,
        avg_views=12.1,
        avg_likes=0.0,
        engagement_rate=0.0,
        engagement_by_views_pct=0.0,
        view_to_subscriber_pct=0.02,
    )
    d_zero = clf.evaluate(genuine_zero)
    assert d_zero.status == FilterStatus.REJECTED
    assert d_zero.tier == "Rejected (Low Engagement)"
    assert any("Actual engagement rate 0.00% below minimum" in r for r in d_zero.failed_criteria)
    assert any("YouTube average views 12 below minimum" in r for r in d_zero.failed_criteria)


def test_youtube_detects_hidden_likes_vs_genuine_zero_watch_page() -> None:
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from outreach_system.discovery.youtube import YouTubeDiscoverer

    yt = YouTubeDiscoverer()
    hidden_html = '"factoidRenderer":{"value":{"simpleText":"N/A"},"label":{"simpleText":"Likes"}}'
    visible_zero_html = '"factoidRenderer":{"value":{"simpleText":"0"},"label":{"simpleText":"Likes"}}'

    def _mock_client(html_body: str):
        resp = MagicMock(status_code=200, text=html_body)
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        return client

    assert asyncio.run(yt._are_video_likes_hidden(_mock_client(hidden_html), "https://www.youtube.com/watch?v=vid1", 5.0)) is True
    assert asyncio.run(yt._are_video_likes_hidden(_mock_client(visible_zero_html), "https://www.youtube.com/watch?v=vid2", 40000.0)) is False
    assert asyncio.run(yt._are_video_likes_hidden(_mock_client("unrelated"), "https://www.youtube.com/watch?v=vid3", 500.0)) is True
    assert asyncio.run(yt._are_video_likes_hidden(_mock_client("unrelated"), "https://www.youtube.com/watch?v=vid4", 10.0)) is False
