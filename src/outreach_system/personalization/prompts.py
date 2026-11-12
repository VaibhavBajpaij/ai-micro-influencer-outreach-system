"""Prompt templates and dynamic collaboration-angle selection for outreach personalization."""

from __future__ import annotations

from outreach_system.config import Settings
from outreach_system.models import InfluencerProfile

COLLABORATION_ANGLES = (
    "Sponsorship",
    "Affiliate campaign",
    "UGC content creation",
    "Brand ambassador program",
    "Paid product placement",
    "Barter collaboration",
)


def select_collaboration_angle(profile: InfluencerProfile) -> str:
    """Choose the best-fit collaboration model based on creator metrics, themes, and style."""
    followers = profile.followers or 0
    eng = profile.engagement_rate or 0.0
    tone = profile.content_tone_style.lower()
    themes = " ".join(profile.content_themes).lower()

    if followers >= 45_000 and ("review" in tone or "gadget" in themes or "desk" in themes):
        return "Paid product placement"
    if followers >= 35_000 and eng >= 0.5:
        return "Sponsorship"
    if "visual" in tone or "aesthetic" in tone or "desk" in themes or "setup" in themes:
        return "UGC content creation"
    if "tutorial" in tone or "workflow" in themes:
        return "Affiliate campaign"
    if followers >= 12_000:
        return "Brand ambassador program"
    return "Barter collaboration"


def build_personalization_prompts(
    profile: InfluencerProfile,
    settings: Settings,
    collaboration_angle: str,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) engineered for strict word counts and real signals."""
    recent_titles = [c.title for c in profile.recent_content[:3]]
    recent_ref = recent_titles[0] if recent_titles else f"your recent {profile.niche} content"

    system_prompt = (
        "You are an expert creator partnerships strategist writing authentic, concise influencer outreach.\n"
        "You MUST return valid JSON with exactly these keys:\n"
        '{\n'
        '  "email_subject": "short compelling subject line (4-9 words)",\n'
        '  "email_pitch": "60-90 words exact body referencing real content and collaboration details",\n'
        '  "instagram_dm": "15-30 words exact short natural DM",\n'
        '  "personalization_signals_used": ["list", "of", "signals"]\n'
        '}\n'
        "Strict rules:\n"
        "1. email_pitch MUST be strictly between 60 and 90 words (count whitespace-separated words carefully).\n"
        "2. instagram_dm MUST be strictly between 15 and 30 words.\n"
        "3. Reference the creator's specific niche, content tone/style, a real recent video/post title, "
        "their audience, the proposed collaboration angle, and the brand value proposition.\n"
        "4. Sound natural and human — NEVER write generic fluff like 'Hi, we would like to collaborate with you.'\n"
        "5. Never invent facts or metrics not provided in the creator context."
    )

    user_prompt = (
        f"Brand: {settings.brand_name}\n"
        f"Product: {settings.brand_product}\n"
        f"Value Proposition: {settings.brand_value_prop}\n"
        f"Sender Name: {settings.sender_name}\n"
        f"Proposed Collaboration Angle: {collaboration_angle}\n\n"
        f"Creator Context:\n"
        f"- Name: {profile.name}\n"
        f"- Platform: {profile.platform}\n"
        f"- Niche: {profile.niche}\n"
        f"- Followers: {profile.followers:,} | Engagement Rate: {profile.engagement_rate or 0:.2f}%\n"
        f"- Content Themes: {', '.join(profile.content_themes)}\n"
        f"- Content Tone/Style: {profile.content_tone_style}\n"
        f"- Relevant Audience: {profile.target_audience_segment}\n"
        f"- Geography: {profile.audience_geography}\n"
        f"- Most Recent Content Title: \"{recent_ref}\"\n"
        f"- Other Recent Titles: {recent_titles[1:] if len(recent_titles) > 1 else 'None'}\n"
        f"- Bio Excerpt: {profile.bio[:280] if profile.bio else 'Not provided'}\n\n"
        "Generate the JSON response now."
    )
    return system_prompt, user_prompt
