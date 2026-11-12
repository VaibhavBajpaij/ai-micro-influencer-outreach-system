"""Multi-provider LLM adapter supporting Gemini, OpenAI, Groq, Anthropic, Ollama, and fallback."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from outreach_system.config import Settings
from outreach_system.models import InfluencerProfile

logger = logging.getLogger(__name__)


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    """Extract a JSON object even if the model wraps it in markdown fences."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        data = json.loads(cleaned[start : end + 1])
        if isinstance(data, dict):
            return data
    raise ValueError("LLM response did not contain a valid JSON object")


class LLMClient:
    """Resolves the active LLM provider and dispatches JSON-structured generation requests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._resolved_provider: str | None = None
        self._resolved_model: str | None = None

    async def resolve_provider(self) -> tuple[str, str]:
        """Determine active provider & model name using configured preference and availability."""
        if self._resolved_provider and self._resolved_model:
            return self._resolved_provider, self._resolved_model

        pref = self.settings.llm_provider.strip().lower()
        if pref == "fallback":
            self._resolved_provider, self._resolved_model = (
                "contextual_fallback",
                "dynamic-nlg-v1",
            )
            return self._resolved_provider, self._resolved_model

        if pref in ("auto", "gemini") and self.settings.gemini_api_key:
            self._resolved_provider, self._resolved_model = ("gemini", self.settings.gemini_model)
            return self._resolved_provider, self._resolved_model

        if pref in ("auto", "openai") and self.settings.openai_api_key:
            self._resolved_provider, self._resolved_model = ("openai", self.settings.openai_model)
            return self._resolved_provider, self._resolved_model

        if pref in ("auto", "groq") and self.settings.groq_api_key:
            self._resolved_provider, self._resolved_model = ("groq", self.settings.groq_model)
            return self._resolved_provider, self._resolved_model

        if pref in ("auto", "anthropic") and self.settings.anthropic_api_key:
            self._resolved_provider, self._resolved_model = (
                "anthropic",
                self.settings.anthropic_model,
            )
            return self._resolved_provider, self._resolved_model

        if pref in ("auto", "ollama"):
            if await self._is_ollama_available():
                self._resolved_provider, self._resolved_model = (
                    "ollama",
                    self.settings.ollama_model,
                )
                return self._resolved_provider, self._resolved_model

        self._resolved_provider, self._resolved_model = (
            "contextual_fallback",
            "dynamic-nlg-v1",
        )
        return self._resolved_provider, self._resolved_model

    async def _is_ollama_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.get(f"{self.settings.ollama_base_url.rstrip('/')}/api/tags")
                if resp.status_code != 200:
                    return False
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                if not models:
                    return False
                target = self.settings.ollama_model
                if any(m == target or m.startswith(f"{target}:") for m in models):
                    return True
                # Use first pulled local model if configured default is absent
                self.settings.ollama_model = models[0]
                return True
        except Exception:
            return False

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        profile: InfluencerProfile,
        collaboration_angle: str,
    ) -> tuple[dict[str, Any], str, str]:
        """Generate structured outreach JSON using active LLM or transparent contextual fallback."""
        provider, model = await self.resolve_provider()
        if provider == "contextual_fallback":
            return (
                self._contextual_fallback(profile, collaboration_angle),
                provider,
                model,
            )

        try:
            if provider == "gemini":
                payload = await self._call_gemini(system_prompt, user_prompt, model)
            elif provider == "openai":
                payload = await self._call_openai(system_prompt, user_prompt, model)
            elif provider == "groq":
                payload = await self._call_groq(system_prompt, user_prompt, model)
            elif provider == "anthropic":
                payload = await self._call_anthropic(system_prompt, user_prompt, model)
            elif provider == "ollama":
                payload = await self._call_ollama(system_prompt, user_prompt, model)
            else:
                payload = self._contextual_fallback(profile, collaboration_angle)
            return payload, provider, model
        except Exception as exc:
            logger.warning(
                "LLM provider %s (%s) failed for %s (%s); falling back safely.",
                provider,
                model,
                profile.name,
                exc,
            )
            return (
                self._contextual_fallback(profile, collaboration_angle),
                f"contextual_fallback (after {provider} error)",
                "dynamic-nlg-v1",
            )

    async def _call_gemini(self, system_prompt: str, user_prompt: str, model: str) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.settings.gemini_api_key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
                response_mime_type="application/json",
            ),
        )
        return _parse_json_object(response.text or "")

    async def _call_openai(self, system_prompt: str, user_prompt: str, model: str) -> dict[str, Any]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        resp = await client.chat.completions.create(
            model=model,
            temperature=0.7,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return _parse_json_object(resp.choices[0].message.content or "")

    async def _call_groq(self, system_prompt: str, user_prompt: str, model: str) -> dict[str, Any]:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.settings.groq_api_key)
        resp = await client.chat.completions.create(
            model=model,
            temperature=0.7,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return _parse_json_object(resp.choices[0].message.content or "")

    async def _call_anthropic(
        self, system_prompt: str, user_prompt: str, model: str
    ) -> dict[str, Any]:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        resp = await client.messages.create(
            model=model,
            max_tokens=500,
            temperature=0.7,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(block.text for block in resp.content if hasattr(block, "text"))
        return _parse_json_object(text)

    async def _call_ollama(self, system_prompt: str, user_prompt: str, model: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{self.settings.ollama_base_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0.7},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
            return _parse_json_object(content)

    @staticmethod
    def _greeting_name(full_name: str) -> str:
        tokens = [t.strip(",:|") for t in full_name.split() if t.strip(",:|")]
        if not tokens:
            return full_name
        if len(tokens) >= 3 and tokens[-2].lower() in {"with", "by"}:
            return tokens[-1]
        brand_prefixes = {
            "ai",
            "tech",
            "mini",
            "pro",
            "the",
            "my",
            "daily",
            "smart",
            "code",
            "dev",
            "data",
            "automation",
        }
        if tokens[0].lower() in brand_prefixes or len(tokens[0]) <= 2:
            head = tokens[:3]
            while len(head) > 1 and head[-1].lower() in {"with", "by", "of", "for", "and"}:
                head.pop()
            return " ".join(head)
        return tokens[0]

    @staticmethod
    def _pick_recent_title(profile: InfluencerProfile, fallback_theme: str) -> str:
        for item in profile.recent_content:
            alnum_words = [w for w in item.title.split() if any(ch.isalnum() for ch in w)]
            if len(alnum_words) >= 3:
                return item.title
        if profile.recent_content and any(ch.isalnum() for ch in profile.recent_content[0].title):
            return profile.recent_content[0].title
        return f"your recent {fallback_theme} breakdown"

    @staticmethod
    def _clean_title_reference(raw_title: str) -> str:
        """Normalize a recent video/post title without awkward mid-stopword cuts or hashtag clutter."""
        cleaned = re.sub(r"#\S+", "", raw_title).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|:,")
        if not cleaned:
            return raw_title.strip()
        words = cleaned.split()
        if len(words) <= 9:
            return cleaned
        for sep in (" | ", " — ", " - ", ": "):
            if sep in cleaned:
                head = cleaned.split(sep, 1)[0].strip()
                head_words = head.split()
                if 3 <= len(head_words) <= 9:
                    return head
        trimmed = words[:8]
        joined = " ".join(trimmed)
        if "(" in joined and ")" not in joined:
            joined = joined.split("(", 1)[0].strip()
            trimmed = joined.split()
        dangling = {"a", "an", "and", "are", "for", "in", "is", "of", "on", "or", "the", "to", "with"}
        while len(trimmed) > 3 and trimmed[-1].lower().strip(",:.") in dangling:
            trimmed.pop()
        return " ".join(trimmed)

    def _contextual_fallback(
        self,
        profile: InfluencerProfile,
        collaboration_angle: str,
    ) -> dict[str, Any]:
        """Multi-angle dynamic NLG fallback used only when no LLM API/Ollama server is active."""
        greet_name = self._greeting_name(profile.name)
        primary_theme = profile.content_themes[0] if profile.content_themes else profile.niche
        secondary_theme = (
            profile.content_themes[1]
            if len(profile.content_themes) > 1
            else profile.niche
        )
        recent_title = self._pick_recent_title(profile, primary_theme)
        short_title = self._clean_title_reference(recent_title)
        tone_lower = profile.content_tone_style.lower()
        brand = self.settings.brand_name
        sender = self.settings.sender_name
        audience_short = profile.target_audience_segment.split("(")[0].strip().lower()
        variant_idx = sum(ord(ch) for ch in profile.id) % 3
        geo_note = (
            f" across {profile.audience_geography}"
            if profile.audience_geography and profile.audience_geography != "Not Available"
            else ""
        )

        if "tutorial" in tone_lower:
            openers = (
                f"Hi {greet_name}, your step-by-step walkthrough in \"{short_title}\" made {primary_theme} super actionable.",
                f"Hi {greet_name}, I really appreciated the practical tutorial pacing in \"{short_title}\" on {primary_theme}.",
                f"Hi {greet_name}, loved how clearly you unpacked {primary_theme} workflows in \"{short_title}\".",
            )
        elif "analytical" in tone_lower:
            openers = (
                f"Hi {greet_name}, your candid breakdown in \"{short_title}\" brought real clarity to {primary_theme}.",
                f"Hi {greet_name}, I enjoyed the grounded testing perspective in \"{short_title}\" around {primary_theme}.",
                f"Hi {greet_name}, your honest evaluation in \"{short_title}\" stood out in {primary_theme}.",
            )
        elif "visual" in tone_lower:
            openers = (
                f"Hi {greet_name}, the clean visual storytelling in \"{short_title}\" really elevates {primary_theme}.",
                f"Hi {greet_name}, loved the studio framing and gear focus in \"{short_title}\" for {primary_theme}.",
                f"Hi {greet_name}, your setup showcase in \"{short_title}\" captured {primary_theme} effortlessly.",
            )
        else:
            openers = (
                f"Hi {greet_name}, loved your {primary_theme} coverage—especially \"{short_title}\".",
                f"Hi {greet_name}, your recent piece \"{short_title}\" offered sharp insights on {primary_theme}.",
                f"Hi {greet_name}, following your work in {primary_theme} and {secondary_theme}, \"{short_title}\" really stood out.",
            )
        opener = openers[variant_idx]

        ctas = (
            f"Can I send over a short 1-page collaboration brief? Best, {sender}",
            f"Would you be open to reviewing our creator brief this week? Best, {sender}",
            f"Mind if I share full details and hardware specs over email? Best, {sender}",
        )
        cta = ctas[variant_idx]

        body_by_angle = {
            "Sponsorship": (
                f"For your {profile.platform} audience of {audience_short}{geo_note}, a dedicated {brand} sponsorship fits naturally alongside {secondary_theme}. "
                f"We build AI-assisted desk docks and provide flat creator fees, early hardware units, and full editorial freedom. {cta}"
            ),
            "Paid product placement": (
                f"Since your {profile.platform} viewers{geo_note} trust real-world testing across {secondary_theme}, we'd love to sponsor a paid {brand} placement in a future roundup. "
                f"We provide early smart-dock units, upfront compensation, and zero scripted talking points. {cta}"
            ),
            "UGC content creation": (
                f"Your visual craft in {primary_theme} and {secondary_theme} matches what our team wants for a paid {brand} UGC series. "
                f"We ship our flagship smart-dock, compensate per deliverable with clear licensing, and keep your workflow authentic. {cta}"
            ),
            "Affiliate campaign": (
                f"Because your {profile.platform} subscribers{geo_note} actively invest in {secondary_theme} tools, a {brand} affiliate partnership pairs well with your guides. "
                f"We seed our AI desk hardware upfront and offer 20% recurring referral rev-share. {cta}"
            ),
            "Brand ambassador program": (
                f"We're building a small cohort of {primary_theme} creators for the {brand} ambassador program. "
                f"Partners receive quarterly gear drops, ongoing retainer support, and custom codes tailored for {audience_short}. {cta}"
            ),
            "Barter collaboration": (
                f"We'd love to support your {primary_theme} channel via a {brand} barter collaboration. "
                f"We'll ship our flagship AI desk dock completely free for your {secondary_theme} studio setup to test honestly whenever it fits naturally. {cta}"
            ),
        }
        email_pitch = f"{opener} {body_by_angle.get(collaboration_angle, body_by_angle['Sponsorship'])}"

        dm_variants = {
            "Sponsorship": (
                f"Hey {greet_name}! Loved \"{short_title}\"—your {primary_theme} audience fits our {brand} sponsorship. Open to seeing a quick brief?",
                f"Hi {greet_name}, enjoyed your {primary_theme} walkthrough in \"{short_title}\"! We're booking {brand} sponsorships this month—interested?",
                f"Hey {greet_name}! Your take in \"{short_title}\" aligns with {brand}'s creator sponsorships for {secondary_theme}. Can I send details?",
            ),
            "Paid product placement": (
                f"Hey {greet_name}, great breakdown in \"{short_title}\"! Would love to fund a paid {brand} placement in an upcoming video. Interested?",
                f"Hi {greet_name}! Loved your honest testing in \"{short_title}\"—open to a paid {brand} gear feature alongside {secondary_theme}?",
                f"Hey {greet_name}, really enjoyed \"{short_title}\"! Would you be open to a paid {brand} hardware integration soon?",
            ),
            "Affiliate campaign": (
                f"Hi {greet_name}! Fan of your {primary_theme} guide in \"{short_title}\". Want to test {brand} gear with 20% recurring affiliate rev-share?",
                f"Hey {greet_name}! Loved \"{short_title}\"—we're seeding {brand} AI desk gear plus 20% recurring affiliate perks for creators. Interested?",
                f"Hi {greet_name}, your {secondary_theme} tutorials like \"{short_title}\" fit our {brand} creator affiliate program. Can I share details?",
            ),
            "UGC content creation": (
                f"Hey {greet_name}, loved \"{short_title}\"! We're booking {primary_theme} creators for a paid {brand} UGC shoot—open to chatting?",
                f"Hi {greet_name}! Your aesthetic in \"{short_title}\" is spot-on for a paid {brand} UGC campaign with gear seeding. Interested?",
                f"Hey {greet_name}, loved the framing in \"{short_title}\"! Would you be open to creating paid UGC around {brand} desk hardware?",
            ),
            "Brand ambassador program": (
                f"Hi {greet_name}! Your {primary_theme} work in \"{short_title}\" fits our {brand} ambassador cohort. Can I send details?",
                f"Hey {greet_name}, enjoyed \"{short_title}\"! We're inviting {secondary_theme} voices into the {brand} creator ambassador program—interested?",
                f"Hi {greet_name}! Loved \"{short_title}\"—would love to share our {brand} ambassador perks and quarterly hardware drops with you.",
            ),
            "Barter collaboration": (
                f"Hey {greet_name}! Enjoyed \"{short_title}\"—we'd love to seed {brand} smart-desk gear to your studio for an honest test. Interested?",
                f"Hi {greet_name}, loved your {primary_theme} piece \"{short_title}\"! Happy to send a free {brand} review unit for your setup—interested?",
                f"Hey {greet_name}! Great work on \"{short_title}\". Want to test a complimentary {brand} AI smart-dock in your {secondary_theme} workflow?",
            ),
        }
        dm_tuple = dm_variants.get(collaboration_angle, dm_variants["Sponsorship"])
        instagram_dm = dm_tuple[variant_idx]

        subject_by_angle = {
            "Sponsorship": f"{brand} x {profile.name}: Creator Sponsorship",
            "Paid product placement": f"Paid gear feature with {brand} for {profile.name}",
            "UGC content creation": f"Paid UGC collaboration: {brand} x {profile.name}",
            "Affiliate campaign": f"Creator affiliate invite from {brand} ({profile.name})",
            "Brand ambassador program": f"{brand} Ambassador Cohort invite for {profile.name}",
            "Barter collaboration": f"Review unit from {brand} for {profile.name}",
        }

        return {
            "email_subject": subject_by_angle.get(
                collaboration_angle, f"Partnership opportunity: {brand} x {profile.name}"
            ),
            "email_pitch": email_pitch,
            "instagram_dm": instagram_dm,
            "personalization_signals_used": [
                f"niche:{profile.niche}",
                f"theme:{primary_theme}",
                f"secondary_theme:{secondary_theme}",
                f"tone:{profile.content_tone_style}",
                f"recent_content:{short_title}",
                f"audience:{profile.target_audience_segment}",
                f"angle:{collaboration_angle}",
            ],
        }
