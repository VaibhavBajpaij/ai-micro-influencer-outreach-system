"""Tests for AI personalization, diversity, subject quality, and strict word bounds."""

from pathlib import Path

import pytest

from outreach_system.config import Settings
from outreach_system.models import ContentItem, FilterStatus, InfluencerProfile
from outreach_system.personalization import (
    MessagePersonalizer,
    count_words,
    select_collaboration_angle,
)
from outreach_system.personalization.generator import enforce_word_bounds


def _sample_profile(
    creator_id: str = "yt_jordan",
    name: str = "Jordan Lin",
    followers: int = 32_000,
    title: str = "Building an Offline AI Coding Desk with Local LLMs",
    themes: list[str] | None = None,
) -> InfluencerProfile:
    return InfluencerProfile(
        run_id="run-test1",
        id=creator_id,
        name=name,
        handle=f"@{creator_id}",
        platform="YouTube",
        profile_url=f"https://www.youtube.com/@{creator_id}",
        followers=followers,
        engagement_rate=1.6,
        niche="Technology & AI Gadgets",
        content_themes=themes or ["AI & Automation Tools", "Desk Setup & Peripherals"],
        contact_email="jordan@linlabs.io",
        instagram_url="https://instagram.com/jordanlin_tech",
        audience_geography="Canada",
        content_tone_style="Educational hands-on tutorials & practical workflow walkthroughs",
        target_audience_segment="Developers and AI productivity builders (18-38)",
        recent_content=[ContentItem(title=title, url="https://youtube.com/watch?v=xyz")],
        status=FilterStatus.QUALIFIED,
        tier="Tier A (Priority Micro)",
        filter_score=84.5,
    )


@pytest.mark.asyncio
async def test_fallback_personalization_enforces_exact_word_bounds_and_clean_subjects() -> None:
    settings = Settings(llm_provider="fallback")
    personalizer = MessagePersonalizer(settings)
    prof = _sample_profile()
    msg = await personalizer.generate_single(prof)

    assert msg.run_id == "run-test1"
    assert 60 <= msg.email_word_count <= 90
    assert count_words(msg.email_pitch) == msg.email_word_count
    assert 15 <= msg.dm_word_count <= 30
    assert count_words(msg.instagram_dm) == msg.dm_word_count
    assert "Building an Offline AI Coding Desk" in msg.email_pitch
    assert "Idea" not in msg.email_subject
    assert msg.llm_provider == "contextual_fallback"
    assert msg.llm_model == "dynamic-nlg-v1"


@pytest.mark.asyncio
async def test_personalization_avoids_repetitive_copy_across_creators() -> None:
    settings = Settings(llm_provider="fallback")
    personalizer = MessagePersonalizer(settings)
    creators = [
        _sample_profile(
            creator_id="yt_alpha_1",
            name="Maya Patel",
            followers=18_000,
            title="Self-Hosted n8n Workflows for Solo Engineers",
            themes=["AI & Automation Tools", "Developer & Productivity Workflows"],
        ),
        _sample_profile(
            creator_id="yt_beta_2",
            name="Liam Brooks",
            followers=22_000,
            title="My Minimal Ergonomic Desk Upgrade Tour",
            themes=["Desk Setup & Peripherals", "Consumer Gadgets & Hardware"],
        ),
        _sample_profile(
            creator_id="yt_gamma_3",
            name="Tech Studio Hub",
            followers=26_000,
            title="Testing 5 Budget Mini PCs for Local Homelab",
            themes=["Laptops, Mini PCs & Homelab", "Smart Home & IoT"],
        ),
    ]
    msgs = await personalizer.generate_for_shortlist(creators)
    pitches = [m.email_pitch for m in msgs]
    dms = [m.instagram_dm for m in msgs]
    subjects = [m.email_subject for m in msgs]

    assert len(set(pitches)) == 3
    assert len(set(dms)) == 3
    assert len(set(subjects)) == 3
    for prof, msg in zip(creators, msgs, strict=True):
        assert 60 <= msg.email_word_count <= 90
        assert 15 <= msg.dm_word_count <= 30
        assert prof.content_themes[0] in msg.email_pitch or prof.content_themes[1] in msg.email_pitch
        assert not msg.email_subject.endswith("Idea")


def test_word_bound_truncation_edge_cases() -> None:
    long_text = (
        "Hi Jordan, loved your recent breakdown of local LLM coding setups. " * 4
        + "We would love to partner on an upcoming release. "
        + "Let me know if you want details."
    )
    bounded = enforce_word_bounds(long_text, min_words=60, max_words=90, padding_phrase="Thanks.")
    wc = count_words(bounded)
    assert 60 <= wc <= 90
    assert bounded.endswith((".", "!", "?"))

    short_text = "Hi Jordan, loved your video."
    expanded = enforce_word_bounds(
        short_text,
        min_words=60,
        max_words=90,
        padding_phrase=(
            "We build AI desk hardware for developers and would love to explore a transparent "
            "paid creator partnership with hardware seeding and zero rigid scripts whenever you have time "
            "to look over a short two-minute brief and product specification sheet together."
        ),
    )
    assert 60 <= count_words(expanded) <= 90


@pytest.mark.asyncio
async def test_llm_retry_guardrail_fixes_out_of_bounds_output(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(llm_provider="openai", openai_api_key="sk-test-dummy")
    personalizer = MessagePersonalizer(settings)
    calls = {"n": 0}

    async def fake_generate_json(sys_prompt, usr_prompt, profile, angle):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                {
                    "email_subject": "Collab",
                    "email_pitch": "Hi Jordan, short pitch.",
                    "instagram_dm": "Hey Jordan!",
                    "personalization_signals_used": ["niche:AI"],
                },
                "openai",
                "gpt-4o-mini",
            )
        email_68 = " ".join(["word"] * 68)
        dm_20 = " ".join(["hey"] * 20)
        return (
            {
                "email_subject": "VoltFlow x Jordan Lin",
                "email_pitch": email_68,
                "instagram_dm": dm_20,
                "personalization_signals_used": ["niche:AI", "recent_content"],
            },
            "openai",
            "gpt-4o-mini",
        )

    monkeypatch.setattr(personalizer.llm, "generate_json", fake_generate_json)
    msg = await personalizer.generate_single(_sample_profile())
    assert calls["n"] == 2
    assert 60 <= msg.email_word_count <= 90
    assert 15 <= msg.dm_word_count <= 30


def test_collaboration_angle_adapts_to_creator_profile() -> None:
    reviewer = _sample_profile(followers=65_000)
    reviewer.content_tone_style = "Analytical product reviews & real-world buyer comparisons"
    assert select_collaboration_angle(reviewer) == "Paid product placement"

    nano_creator = _sample_profile(followers=6_000, themes=["Consumer Gadgets & Hardware"])
    nano_creator.content_tone_style = "Creator-led commentary & niche community content"
    assert select_collaboration_angle(nano_creator) == "Barter collaboration"


@pytest.mark.asyncio
async def test_generation_mode_distinguishes_llm_from_contextual_fallback_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from outreach_system.storage import OutreachDatabase

    # 1. Genuine LLM response sets generation_mode == "LLM"
    settings_llm = Settings(llm_provider="gemini", gemini_api_key="AIza-test-dummy")
    p_llm = MessagePersonalizer(settings_llm)

    async def fake_llm_json(sys_prompt, usr_prompt, profile, angle):
        return (
            {
                "email_subject": "VoltFlow x Jordan Lin",
                "email_pitch": " ".join(["hello"] * 68),
                "instagram_dm": " ".join(["hey"] * 18),
                "personalization_signals_used": ["niche:AI"],
            },
            "gemini",
            "gemini-2.0-flash",
        )

    monkeypatch.setattr(p_llm.llm, "generate_json", fake_llm_json)
    msg_llm = await p_llm.generate_single(_sample_profile(creator_id="yt_llm1"))
    assert msg_llm.generation_mode == "LLM"
    assert msg_llm.llm_provider == "gemini"

    # 2. Failed/unavailable LLM falls back deterministically with generation_mode == "contextual_fallback"
    settings_fb = Settings(_env_file=None, llm_provider="auto", gemini_api_key="", openai_api_key="", groq_api_key="", anthropic_api_key="")
    p_fb = MessagePersonalizer(settings_fb)
    msg_fb = await p_fb.generate_single(_sample_profile(creator_id="yt_fb1"))
    assert msg_fb.generation_mode == "contextual_fallback"
    assert msg_fb.llm_provider == "contextual_fallback"

    # 3. Persisting in SQLite preserves explicit generation_mode
    db = OutreachDatabase(tmp_path / "genmode.db")
    prof_llm = _sample_profile(creator_id="yt_llm1")
    prof_fb = _sample_profile(creator_id="yt_fb1")
    db.upsert_influencers([prof_llm, prof_fb], run_id="run-test1", mark_previous_inactive=True)
    db.save_run_messages([msg_llm, msg_fb], run_id="run-test1")
    loaded = {m.influencer_id: m for m in db.get_messages(current_only=True)}
    assert loaded["yt_llm1"].generation_mode == "LLM"
    assert loaded["yt_fb1"].generation_mode == "contextual_fallback"
