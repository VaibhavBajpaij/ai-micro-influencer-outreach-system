"""Base protocol and shared parsing helpers for discovery sources."""

from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlparse

from outreach_system.config import NichePreset
from outreach_system.models import InfluencerRaw

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
COMPACT_NUM_RE = re.compile(r"([\d,.]+)\s*([KMB])?", re.IGNORECASE)


class InfluencerDiscoverer(Protocol):
    """Protocol implemented by every platform discovery connector."""

    platform_name: str

    async def discover(self, preset: NichePreset, limit: int = 40) -> list[InfluencerRaw]:
        """Fetch raw creator records with real public metrics and recent content."""
        ...


def parse_compact_number(text: str | None) -> int | None:
    """Convert compact social strings such as '16.4K subscribers' or '1.2M' to integer."""
    if not text:
        return None
    match = COMPACT_NUM_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group(2) or "").upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(round(value * multiplier))


def unwrap_youtube_redirect(url: str) -> str:
    """Resolve YouTube /redirect?q=... links to the underlying destination URL."""
    try:
        parsed = urlparse(url)
        if "youtube.com" in parsed.netloc and parsed.path == "/redirect":
            q_vals = parse_qs(parsed.query).get("q")
            if q_vals:
                return unquote(q_vals[0])
    except Exception:
        pass
    return url


def extract_urls(text: str) -> list[str]:
    """Extract clean unique HTTP(S) URLs from free-form bio or description text."""
    if not text:
        return []
    seen: list[str] = []
    for raw in URL_RE.findall(text):
        cleaned = raw.rstrip(".,;:!?")
        cleaned = unwrap_youtube_redirect(cleaned)
        if cleaned not in seen:
            seen.append(cleaned)
    return seen

