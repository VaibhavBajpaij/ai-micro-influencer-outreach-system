"""Personalization orchestrator with strict word-count validation and retry guardrails."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from outreach_system.config import Settings
from outreach_system.models import InfluencerProfile, PersonalizedMessage, resolve_generation_mode
from outreach_system.personalization.llm_client import LLMClient
from outreach_system.personalization.prompts import (
    build_personalization_prompts,
    select_collaboration_angle,
)


def count_words(text: str) -> int:
    """Count whitespace-delimited words consistently across emails and DMs."""
    return len([w for w in text.strip().split() if w])


def enforce_word_bounds(
    text: str,
    min_words: int,
    max_words: int,
    padding_phrase: str,
) -> str:
    """Guarantee strict word-count compliance while preserving complete sentence boundaries."""
    words = [w for w in text.strip().split() if w]
    if len(words) > max_words:
        # Prefer ending at a complete sentence boundary within [min_words, max_words]
        for idx in range(max_words - 1, min_words - 1, -1):
            if words[idx].endswith((".", "!", "?")):
                return " ".join(words[: idx + 1])
        trimmed = words[:max_words]
        joined = " ".join(trimmed).rstrip(",;:-")
        if not joined.endswith((".", "!", "?")):
            joined += "."
        return joined
    if len(words) < min_words:
        pad_words = [w for w in padding_phrase.strip().split() if w]
        needed = min_words - len(words)
        combined = words + pad_words[: max(needed, 0)]
        # If padding phrase was shorter than deficit, add clean closing note
        if len(combined) < min_words:
            extra = "Let me know if you would like to review the collaboration details and timeline together soon.".split()
            combined.extend(extra[: min_words - len(combined)])
        return " ".join(combined[:max_words])
    return " ".join(words)


class MessagePersonalizer:
    """Generates dynamic Email pitches (60-90w) and Instagram DMs (15-30w) for shortlisted creators."""

    def __init__(self, settings: Settings, concurrency: int = 5) -> None:
        self.settings = settings
        self.llm = LLMClient(settings)
        self.concurrency = concurrency

    async def generate_for_shortlist(
        self, profiles: list[InfluencerProfile]
    ) -> list[PersonalizedMessage]:
        """Generate personalized outreach messages for every qualified influencer."""
        sem = asyncio.Semaphore(self.concurrency)

        async def run_one(profile: InfluencerProfile) -> PersonalizedMessage:
            async with sem:
                return await self.generate_single(profile)

        return await asyncio.gather(*(run_one(p) for p in profiles))

    async def generate_single(self, profile: InfluencerProfile) -> PersonalizedMessage:
        angle = select_collaboration_angle(profile)
        sys_prompt, usr_prompt = build_personalization_prompts(profile, self.settings, angle)
        payload, provider, model = await self.llm.generate_json(
            sys_prompt, usr_prompt, profile, angle
        )

        email_pitch = str(payload.get("email_pitch") or "").strip()
        instagram_dm = str(payload.get("instagram_dm") or "").strip()
        subject = str(payload.get("email_subject") or f"{self.settings.brand_name} x {profile.name}").strip()

        # Automatic LLM refinement retry if active LLM missed strict word bounds
        if not provider.startswith("contextual_fallback") and (
            not (60 <= count_words(email_pitch) <= 90) or not (15 <= count_words(instagram_dm) <= 30)
        ):
            retry_prompt = (
                f"{usr_prompt}\n\n"
                f"CORRECTION NEEDED: Your previous email_pitch had {count_words(email_pitch)} words "
                f"(must be 60-90) and instagram_dm had {count_words(instagram_dm)} words (must be 15-30). "
                "Rewrite strictly within those word limits and return valid JSON."
            )
            retry_payload, provider, model = await self.llm.generate_json(
                sys_prompt, retry_prompt, profile, angle
            )
            email_pitch = str(retry_payload.get("email_pitch") or email_pitch).strip()
            instagram_dm = str(retry_payload.get("instagram_dm") or instagram_dm).strip()
            subject = str(retry_payload.get("email_subject") or subject).strip()
            payload = retry_payload

        email_pitch = enforce_word_bounds(
            email_pitch,
            min_words=60,
            max_words=90,
            padding_phrase=(
                f"We would love to share full creative flexibility, product perks, and transparent "
                f"compensation tailored for your {profile.niche} community whenever convenient."
            ),
        )
        instagram_dm = enforce_word_bounds(
            instagram_dm,
            min_words=15,
            max_words=30,
            padding_phrase=f"Would love to send quick {self.settings.brand_name} creator collaboration details!",
        )

        signals = payload.get("personalization_signals_used")
        if not isinstance(signals, list) or not signals:
            signals = [
                f"niche:{profile.niche}",
                f"tone:{profile.content_tone_style}",
                f"angle:{angle}",
            ]

        return PersonalizedMessage(
            run_id=profile.run_id,
            influencer_id=profile.id,
            influencer_name=profile.name,
            platform=profile.platform,
            contact_email=profile.contact_email,
            instagram_url=profile.instagram_url,
            collaboration_angle=angle,
            email_subject=subject,
            email_pitch=email_pitch,
            email_word_count=count_words(email_pitch),
            instagram_dm=instagram_dm,
            dm_word_count=count_words(instagram_dm),
            personalization_signals_used=[str(s) for s in signals],
            generation_mode=resolve_generation_mode(provider),
            llm_provider=provider,
            llm_model=model,
            generated_at=datetime.now(UTC).isoformat(),
        )
