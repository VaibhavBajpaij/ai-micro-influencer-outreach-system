"""Bluesky public AppView API discoverer (no auth required)."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote_plus

import httpx

from outreach_system.config import NichePreset
from outreach_system.discovery.base import extract_urls
from outreach_system.models import ContentItem, InfluencerRaw

logger = logging.getLogger(__name__)

BSKY_API = "https://public.api.bsky.app/xrpc"


class BlueskyDiscoverer:
    """Discovers creators on Bluesky via public AppView search, profiles, and author feeds."""

    platform_name = "Bluesky"

    def __init__(self, concurrency: int = 6, timeout_seconds: float = 12.0) -> None:
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds

    async def discover(self, preset: NichePreset, limit: int = 20) -> list[InfluencerRaw]:
        """Discover up to `limit` unique Bluesky creators for `preset`."""
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            stubs: dict[str, str] = {}
            for query in preset.bluesky_queries:
                if len(stubs) >= limit + 10:
                    break
                try:
                    resp = await client.get(
                        f"{BSKY_API}/app.bsky.actor.searchActors?q={quote_plus(query)}&limit=15"
                    )
                    if resp.status_code != 200:
                        logger.warning("Bluesky search status %s for '%s'", resp.status_code, query)
                        continue
                    for actor in resp.json().get("actors", []):
                        handle = actor.get("handle")
                        if handle and handle not in stubs:
                            stubs[handle] = query
                except Exception as exc:
                    logger.warning("Bluesky search failed for '%s': %s", query, exc)

            candidate_handles = list(stubs.items())[:limit]
            sem = asyncio.Semaphore(self.concurrency)

            async def fetch_one(handle: str, query: str) -> InfluencerRaw | None:
                async with sem:
                    return await self._fetch_profile_and_feed(client, handle, query)

            results = await asyncio.gather(
                *(fetch_one(h, q) for h, q in candidate_handles),
                return_exceptions=False,
            )
            return [r for r in results if r is not None]

    async def _fetch_profile_and_feed(
        self,
        client: httpx.AsyncClient,
        handle: str,
        discovery_query: str,
    ) -> InfluencerRaw | None:
        prof_resp, feed_resp = await asyncio.gather(
            client.get(f"{BSKY_API}/app.bsky.actor.getProfile?actor={quote_plus(handle)}"),
            client.get(
                f"{BSKY_API}/app.bsky.feed.getAuthorFeed?actor={quote_plus(handle)}&limit=10&filter=posts_no_replies"
            ),
            return_exceptions=True,
        )
        if not isinstance(prof_resp, httpx.Response) or prof_resp.status_code != 200:
            return None

        prof = prof_resp.json()
        followers = prof.get("followersCount")
        bio = (prof.get("description") or "").strip()
        name = (prof.get("displayName") or "").strip() or handle

        recent_content: list[ContentItem] = []
        interactions: list[int] = []
        likes: list[int] = []

        if isinstance(feed_resp, httpx.Response) and feed_resp.status_code == 200:
            for item in feed_resp.json().get("feed", []):
                # Skip reposts by other authors so we only measure creator's own engagement
                if item.get("reason"):
                    continue
                post = item.get("post") or {}
                record = post.get("record") or {}
                text = (record.get("text") or "").strip()
                if not text:
                    continue
                like_c = int(post.get("likeCount") or 0)
                repost_c = int(post.get("repostCount") or 0)
                reply_c = int(post.get("replyCount") or 0)
                quote_c = int(post.get("quoteCount") or 0)
                total = like_c + repost_c + reply_c + quote_c
                interactions.append(total)
                likes.append(like_c)

                uri = str(post.get("uri") or "")
                rkey = uri.split("/")[-1] if "/" in uri else ""
                post_url = (
                    f"https://bsky.app/profile/{handle}/post/{rkey}"
                    if rkey
                    else f"https://bsky.app/profile/{handle}"
                )
                first_line = text.splitlines()[0][:120]
                recent_content.append(
                    ContentItem(
                        title=first_line,
                        url=post_url,
                        published_at=record.get("createdAt"),
                        likes=like_c,
                        comments_or_replies=reply_c,
                        reposts=repost_c,
                        description_snippet=text[:280],
                    )
                )

        avg_interactions = (
            round(sum(interactions) / len(interactions), 1) if interactions else None
        )
        avg_likes = round(sum(likes) / len(likes), 1) if likes else None
        engagement_rate = None
        if followers and followers > 0 and avg_interactions is not None:
            engagement_rate = round((avg_interactions / followers) * 100.0, 2)

        recent_content.sort(key=lambda c: c.published_at or "", reverse=True)
        return InfluencerRaw(
            id=f"bsky_{handle}",
            name=name,
            handle=f"@{handle}",
            platform=self.platform_name,
            profile_url=f"https://bsky.app/profile/{handle}",
            followers=int(followers) if followers is not None else None,
            avg_likes=avg_likes,
            avg_interactions=avg_interactions,
            engagement_rate=engagement_rate,
            engagement_formula="avg(likes + reposts + replies + quotes) / followers * 100",
            bio=bio,
            country=None,
            discovery_query=discovery_query,
            external_links=extract_urls(bio),
            recent_content=recent_content[:8],
            last_post_date=recent_content[0].published_at if recent_content else None,
            raw_video_descriptions=[c.description_snippet for c in recent_content if c.description_snippet],
        )
