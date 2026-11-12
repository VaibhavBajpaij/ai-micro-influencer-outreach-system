"""Enriches influencer profiles with verified public emails, themes, socials, and style cues."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from outreach_system.config import NichePreset
from outreach_system.discovery.base import extract_urls
from outreach_system.models import (
    FilterDecision,
    FilterStatus,
    InfluencerProfile,
    InfluencerRaw,
)

logger = logging.getLogger(__name__)

STANDARD_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z0-9](?:[A-Za-z0-9._%+\-]*[A-Za-z0-9])?@[A-Za-z0-9.\-]+\.[A-Za-z]{2,15})(?![A-Za-z0-9._%+\-])"
)
OBFUSCATED_EMAIL_RE = re.compile(
    r"([A-Za-z0-9](?:[A-Za-z0-9._%+\-]*[A-Za-z0-9])?)\s*(?:\[\s*at\s*\]|\(\s*at\s*\))\s*([A-Za-z0-9.\-]+)\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\))\s*([A-Za-z]{2,15})",
    re.IGNORECASE,
)

IGNORED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "yourcompany.com",
    "sentry.io",
    "wixpress.com",
    "youtube.com",
    "google.com",
    "gstatic.com",
    "linktr.ee",
    "beacons.ai",
    "carrd.co",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "bsky.app",
    "blueskyweb.xyz",
    "cloudflare.com",
}

IGNORED_EMAIL_PREFIXES = ("noreply", "no-reply", "donotreply", "privacy", "legal", "dmca", "abuse")
IMAGE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "css", "js"}

IG_MENTION_RE = re.compile(
    r"(?:instagram|insta|\big\b)\s*[:\-|]?\s*@?([A-Za-z0-9_.]{2,30})",
    re.IGNORECASE,
)
TIKTOK_MENTION_RE = re.compile(
    r"(?:tiktok|\btt\b)\s*[:\-|]?\s*@?([A-Za-z0-9_.]{2,30})",
    re.IGNORECASE,
)

SOCIAL_DOMAINS = {
    "instagram.com",
    "www.instagram.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "tiktok.com",
    "www.tiktok.com",
    "twitter.com",
    "x.com",
    "bsky.app",
    "facebook.com",
    "linkedin.com",
    "discord.gg",
    "discord.com",
    "t.me",
    "twitch.tv",
    "amazon.com",
    "amazon.in",
    "amzn.to",
    "link.amazon",
    "geni.us",
    "bit.ly",
    "dub.sh",
    "tinyurl.com",
    "rstyle.me",
    "howl.me",
    "tidd.ly",
}

LINK_HUB_DOMAINS = ("linktr.ee", "beacons.ai", "bio.link", "lnk.bio", "bento.me", "solo.to", "hoo.be")


def is_valid_public_email(candidate: str) -> bool:
    """Filter out placeholder, noreply, platform-support, and asset false-positive emails."""
    cleaned = candidate.strip().lower()
    if "@" not in cleaned or ".." in cleaned:
        return False
    local, domain = cleaned.split("@", 1)
    if not local or not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1]
    if tld in IMAGE_TLDS:
        return False
    if any(local.startswith(prefix) for prefix in IGNORED_EMAIL_PREFIXES):
        return False
    if domain in IGNORED_EMAIL_DOMAINS:
        return False
    if any(domain.endswith(f".{ignored}") for ignored in IGNORED_EMAIL_DOMAINS):
        return False
    return True


def extract_emails_from_text(text: str) -> list[str]:
    """Extract only explicitly present, non-placeholder public emails from text."""
    if not text:
        return []
    found: list[str] = []
    for match in STANDARD_EMAIL_RE.findall(text):
        email = match.strip()
        if is_valid_public_email(email) and email.lower() not in {e.lower() for e in found}:
            found.append(email)
    for user, host, tld in OBFUSCATED_EMAIL_RE.findall(text):
        email = f"{user.strip()}@{host.strip()}.{tld.strip()}"
        if is_valid_public_email(email) and email.lower() not in {e.lower() for e in found}:
            found.append(email)
    return found


def _is_safe_public_http_url(url: str) -> bool:
    """Reject non-HTTP(S) schemes and loopback/private IP targets before crawling."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass
    return True


def _extract_social_links(raw: InfluencerRaw, extra_urls: list[str]) -> dict[str, str]:
    """Extract Instagram, YouTube, TikTok, and personal website URLs cleanly."""
    all_urls = list(dict.fromkeys([*raw.external_links, *extra_urls]))
    ig_url = "Not Found"
    yt_url = raw.profile_url if raw.platform == "YouTube" else "Not Found"
    tt_url = "Not Found"
    website = "Not Found"
    link_hub_fallback = "Not Found"

    for url in all_urls:
        lower = url.lower()
        try:
            host = (urlparse(lower).hostname or "").removeprefix("www.")
        except Exception:
            continue
        if not host:
            continue
        if "instagram.com" in host and ig_url == "Not Found" and "/p/" not in lower:
            ig_url = url.split("?")[0].rstrip("/")
        elif ("youtube.com" in host or "youtu.be" in host) and yt_url == "Not Found":
            if "/@" in lower or "/channel/" in lower or "/c/" in lower:
                yt_url = url.split("?")[0].rstrip("/")
        elif "tiktok.com" in host and tt_url == "Not Found":
            tt_url = url.split("?")[0].rstrip("/")
        elif host not in SOCIAL_DOMAINS and not any(
            skip in host for skip in ("youtu.be", "youtube.com", "instagram.com", "tiktok.com", "amazon.", "amzn.to")
        ):
            if host != "bsky.app":
                if any(hub in host for hub in LINK_HUB_DOMAINS):
                    if link_hub_fallback == "Not Found":
                        link_hub_fallback = url
                elif website == "Not Found":
                    website = url

    if website == "Not Found" and link_hub_fallback != "Not Found":
        website = link_hub_fallback

    # Fallback: check bio text for explicit "IG: @handle" or "TikTok: @handle"
    if ig_url == "Not Found":
        m_ig = IG_MENTION_RE.search(raw.bio)
        if m_ig and m_ig.group(1).lower() not in {"com", "link", "bio", "handle"}:
            ig_url = f"https://instagram.com/{m_ig.group(1)}"
    if tt_url == "Not Found":
        m_tt = TIKTOK_MENTION_RE.search(raw.bio)
        if m_tt and m_tt.group(1).lower() not in {"com", "link", "bio", "handle"}:
            tt_url = f"https://tiktok.com/@{m_tt.group(1)}"

    return {
        "instagram_url": ig_url,
        "youtube_url": yt_url,
        "tiktok_url": tt_url,
        "website": website,
    }


def _classify_themes(raw: InfluencerRaw, preset: NichePreset) -> list[str]:
    corpus = " ".join(
        [raw.name, raw.bio, *(item.title for item in raw.recent_content[:8])]
    ).lower()
    scored: list[tuple[int, str]] = []
    for theme_name, keywords in preset.theme_keywords.items():
        hits = sum(1 for kw in keywords if kw in corpus)
        if hits > 0:
            scored.append((hits, theme_name))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored:
        return [name for _, name in scored[:3]]
    return [preset.display_name]


def _infer_content_tone_style(raw: InfluencerRaw) -> str:
    corpus = " ".join(
        [raw.bio, *(item.title for item in raw.recent_content[:8])]
    ).lower()
    if any(w in corpus for w in ("tutorial", "guide", "how to", "workflow", "setup", "build")):
        return "Educational hands-on tutorials & practical workflow walkthroughs"
    if any(w in corpus for w in ("vs", "comparison", "worth it", "honest", "long term", "review")):
        return "Analytical product reviews & real-world buyer comparisons"
    if any(w in corpus for w in ("minimal", "aesthetic", "desk tour", "workspace", "unboxing")):
        return "Visual showcase, desk aesthetic & gear unboxing storytelling"
    if any(w in corpus for w in ("news", "weekly", "update", "launch", "open source")):
        return "Fast-paced industry commentary & curated ecosystem updates"
    return "Creator-led commentary & niche community content"


class ProfileEnricher:
    """Enriches influencer profiles with verified email OSINT, themes, tone, and socials."""

    def __init__(self, preset: NichePreset, concurrency: int = 6, timeout_seconds: float = 8.0) -> None:
        self.preset = preset
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds

    async def enrich_all(
        self,
        items: list[tuple[InfluencerRaw, FilterDecision]],
        crawl_websites_for_rejected: bool = False,
    ) -> list[InfluencerProfile]:
        """Enrich all discovered profiles; crawl external links deeply for qualified creators."""
        sem = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; OutreachEnricher/1.0)"},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:

            async def enrich_one(raw: InfluencerRaw, decision: FilterDecision) -> InfluencerProfile:
                should_crawl = (
                    decision.status == FilterStatus.QUALIFIED or crawl_websites_for_rejected
                )
                async with sem:
                    return await self._enrich_single(client, raw, decision, crawl_external=should_crawl)

            return await asyncio.gather(*(enrich_one(r, d) for r, d in items))

    async def _enrich_single(
        self,
        client: httpx.AsyncClient,
        raw: InfluencerRaw,
        decision: FilterDecision,
        crawl_external: bool,
    ) -> InfluencerProfile:
        email = "Not Found"
        email_source = "none"
        extra_urls: list[str] = []

        # Step 1: Bio email extraction
        bio_emails = extract_emails_from_text(raw.bio)
        if bio_emails:
            email = bio_emails[0]
            email_source = "channel_bio"

        # Step 2: Recent video/post description email extraction
        if email == "Not Found":
            for desc in raw.raw_video_descriptions[:10]:
                desc_emails = extract_emails_from_text(desc)
                if desc_emails:
                    email = desc_emails[0]
                    email_source = "video_description"
                    break

        # Step 3: Safe public Linktree / personal website crawl for email & cross-platform socials
        if crawl_external and raw.external_links:
            candidate_urls = sorted(
                [u for u in raw.external_links if _is_safe_public_http_url(u)],
                key=lambda u: 0 if any(hub in u.lower() for hub in LINK_HUB_DOMAINS) else 1,
            )
            # Skip heavy social sites that require JS/auth or do not expose business email reliably
            crawl_targets = [
                u
                for u in candidate_urls
                if not any(
                    s in u.lower()
                    for s in (
                        "youtube.com",
                        "youtu.be",
                        "instagram.com",
                        "tiktok.com",
                        "twitter.com",
                        "x.com",
                        "amazon.",
                        "amzn.to",
                        "link.amazon",
                        "geni.us",
                        "dub.sh",
                        "bit.ly",
                        "discord.",
                    )
                )
            ][:2]
            for target_url in crawl_targets:
                found_emails, found_links = await self._crawl_public_page(client, target_url)
                extra_urls.extend(found_links)
                if email == "Not Found" and found_emails:
                    email = found_emails[0]
                    email_source = "external_website"
                    break

        socials = _extract_social_links(raw, extra_urls)
        themes = _classify_themes(raw, self.preset)
        tone_style = _infer_content_tone_style(raw)
        has_valid_email = email != "Not Found" and is_valid_public_email(email)
        is_qualified = decision.status == FilterStatus.QUALIFIED
        outreach_ready = is_qualified and has_valid_email
        if outreach_ready:
            readiness_label = "Outreach-Ready (Email Verified)"
        elif is_qualified:
            readiness_label = "Qualified (Not Email-Ready - DM Only)"
        else:
            readiness_label = "Rejected (Not Shortlisted)"

        return InfluencerProfile(
            id=raw.id,
            name=raw.name,
            handle=raw.handle,
            platform=raw.platform,
            profile_url=raw.profile_url,
            followers=raw.followers,
            engagement_rate=raw.engagement_rate,
            niche=self.preset.display_name,
            content_themes=themes,
            contact_email=email,
            instagram_url=socials["instagram_url"],
            youtube_url=socials["youtube_url"],
            tiktok_url=socials["tiktok_url"],
            website=socials["website"],
            audience_age="Not Available (Private Analytics)",
            audience_gender="Not Available (Private Analytics)",
            audience_geography=raw.country if raw.country else "Not Available",
            bio=raw.bio,
            avg_views=raw.avg_views,
            avg_likes=raw.avg_likes,
            engagement_by_views_pct=raw.engagement_by_views_pct,
            view_to_subscriber_pct=raw.view_to_subscriber_pct,
            engagement_formula=raw.engagement_formula,
            content_tone_style=tone_style,
            target_audience_segment=self.preset.default_audience_segment,
            email_source=email_source,
            last_post_date=raw.last_post_date,
            discovery_query=raw.discovery_query,
            recent_content=raw.recent_content,
            status=decision.status,
            outreach_ready=outreach_ready,
            outreach_readiness=readiness_label,
            tier=decision.tier,
            filter_score=decision.filter_score,
            niche_relevance_score=decision.niche_relevance_score,
            brand_fit_score=decision.brand_fit_score,
            passed_criteria=decision.passed_criteria,
            failed_criteria=decision.failed_criteria,
            filter_summary=decision.filter_summary,
            identity_cluster_id=raw.id,
        )

    async def _crawl_public_page(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[list[str], list[str]]:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return [], []
            content_type = (resp.headers.get("content-type") or "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                return [], []
            html = resp.text[:300_000]
            soup = BeautifulSoup(html, "html.parser")

            emails: list[str] = []
            links: list[str] = []
            for a_tag in soup.find_all("a", href=True):
                href = str(a_tag["href"]).strip()
                if href.lower().startswith("mailto:"):
                    addr = href[7:].split("?")[0].strip()
                    if is_valid_public_email(addr) and addr.lower() not in {e.lower() for e in emails}:
                        emails.append(addr)
                elif href.startswith("http"):
                    links.append(href)

            visible_text = soup.get_text(" ", strip=True)
            for addr in extract_emails_from_text(visible_text):
                if addr.lower() not in {e.lower() for e in emails}:
                    emails.append(addr)
            for u in extract_urls(visible_text):
                if u not in links:
                    links.append(u)
            return emails, links[:25]
        except Exception as exc:
            logger.debug("External enrichment crawl skipped for %s: %s", url, exc)
            return [], []


def annotate_cross_platform_identities(profiles: list[InfluencerProfile]) -> list[InfluencerProfile]:
    """Conservatively detect cross-platform identity links without merging name-only matches."""
    for p in profiles:
        if not p.identity_cluster_id:
            p.identity_cluster_id = p.id

    def _norm_name(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def _has_verified_url_link(a: InfluencerProfile, b: InfluencerProfile) -> bool:
        a_url = a.profile_url.lower().rstrip("/")
        a_handle = a.handle.lower().lstrip("@")
        b_text = f"{b.bio} {b.website} {b.youtube_url} {b.instagram_url}".lower()
        if a_url and a_url in b_text:
            return True
        if a_handle and len(a_handle) >= 4:
            if f"youtube.com/@{a_handle}" in b_text or f"bsky.app/profile/{a_handle}" in b_text:
                return True
        return False

    for i, p1 in enumerate(profiles):
        for p2 in profiles[i + 1 :]:
            if p1.platform == p2.platform:
                continue

            verified_reason = ""
            if (
                p1.contact_email != "Not Found"
                and p2.contact_email != "Not Found"
                and p1.contact_email.strip().lower() == p2.contact_email.strip().lower()
            ):
                verified_reason = f"shared verified email ({p1.contact_email})"
            elif _has_verified_url_link(p1, p2) or _has_verified_url_link(p2, p1):
                verified_reason = "explicit cross-profile URL link"

            if verified_reason:
                cluster = f"cluster_{min(p1.id, p2.id)}_{max(p1.id, p2.id)}"
                p1.identity_cluster_id = cluster
                p2.identity_cluster_id = cluster
                p1.cross_platform_note = (
                    f"Verified cross-platform identity linked with {p2.platform} ({p2.id}) "
                    f"via {verified_reason}; platform-specific profiles preserved."
                )
                p2.cross_platform_note = (
                    f"Verified cross-platform identity linked with {p1.platform} ({p1.id}) "
                    f"via {verified_reason}; platform-specific profiles preserved."
                )
            else:
                n1, n2 = _norm_name(p1.name), _norm_name(p2.name)
                if len(n1) >= 4 and n1 == n2:
                    if not p1.cross_platform_note:
                        p1.cross_platform_note = (
                            f"Uncertain display-name similarity with {p2.platform} ({p2.id}); "
                            "kept separate without verified link."
                        )
                    if not p2.cross_platform_note:
                        p2.cross_platform_note = (
                            f"Uncertain display-name similarity with {p1.platform} ({p1.id}); "
                            "kept separate without verified link."
                        )
    return profiles
