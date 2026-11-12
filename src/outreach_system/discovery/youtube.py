"""YouTube public search, channel About metadata, and Atom RSS feed discoverer."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus

import httpx

from outreach_system.config import NichePreset
from outreach_system.discovery.base import (
    extract_urls,
    parse_compact_number,
    unwrap_youtube_redirect,
)
from outreach_system.models import ContentItem, InfluencerRaw

logger = logging.getLogger(__name__)

YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

YT_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def extract_yt_initial_data(html: str) -> dict[str, Any] | None:
    """Safely parse YouTube's embedded ytInitialData JSON object from HTML."""
    for marker in ("var ytInitialData = ", 'window["ytInitialData"] = '):
        idx = html.find(marker)
        if idx != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(html[idx + len(marker) :])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _collect_channel_stubs(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Walk ytInitialData for unique (channel_id, display_name) pairs from video results."""
    found: list[tuple[str, str]] = []
    seen_ids: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "videoRenderer" in node:
                vr = node["videoRenderer"]
                runs = vr.get("ownerText", {}).get("runs") or vr.get("longBylineText", {}).get("runs") or []
                for run in runs:
                    browse = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
                    cid = browse.get("browseId", "")
                    name = (run.get("text") or "").strip()
                    if cid.startswith("UC") and cid not in seen_ids:
                        seen_ids.add(cid)
                        found.append((cid, name))
            elif "channelRenderer" in node:
                cr = node["channelRenderer"]
                cid = cr.get("channelId", "")
                name = (cr.get("title", {}).get("simpleText") or "").strip()
                if cid.startswith("UC") and cid not in seen_ids:
                    seen_ids.add(cid)
                    found.append((cid, name))
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _find_first_key(node: Any, key: str) -> Any:
    """Depth-first search for the first occurrence of `key` in nested dicts/lists."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            res = _find_first_key(v, key)
            if res is not None:
                return res
    elif isinstance(node, list):
        for item in node:
            res = _find_first_key(item, key)
            if res is not None:
                return res
    return None


def _extract_about_links(vm: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for item in vm.get("links", []) or []:
        model = item.get("channelExternalLinkViewModel", {})
        link_obj = model.get("link", {})
        runs = link_obj.get("commandRuns", []) or []
        extracted = ""
        for run in runs:
            url = (
                run.get("onTap", {})
                .get("innertubeCommand", {})
                .get("commandMetadata", {})
                .get("webCommandMetadata", {})
                .get("url", "")
            )
            if url:
                extracted = unwrap_youtube_redirect(url)
                break
        if not extracted:
            raw_text = (link_obj.get("content") or "").strip()
            if raw_text:
                extracted = raw_text if raw_text.startswith("http") else f"https://{raw_text}"
        if extracted and extracted not in links:
            links.append(extracted)
    return links


def _parse_rss_feed(xml_text: str) -> tuple[str | None, list[ContentItem], list[str]]:
    """Parse up to 12 recent videos from YouTube's official public channel Atom feed."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, [], []

    channel_author = None
    author_el = root.find("atom:author/atom:name", YT_NS)
    if author_el is not None and author_el.text:
        channel_author = author_el.text.strip()

    items: list[ContentItem] = []
    descriptions: list[str] = []
    for entry in root.findall("atom:entry", YT_NS)[:12]:
        vid_el = entry.find("yt:videoId", YT_NS)
        title_el = entry.find("atom:title", YT_NS)
        pub_el = entry.find("atom:published", YT_NS)
        stats_el = entry.find(".//media:statistics", YT_NS)
        rating_el = entry.find(".//media:starRating", YT_NS)
        desc_el = entry.find(".//media:description", YT_NS)

        vid = vid_el.text.strip() if vid_el is not None and vid_el.text else ""
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        if not vid or not title:
            continue

        views = None
        if stats_el is not None and stats_el.attrib.get("views"):
            try:
                views = int(stats_el.attrib["views"])
            except ValueError:
                views = None

        likes = None
        if rating_el is not None and rating_el.attrib.get("count"):
            try:
                likes = int(rating_el.attrib["count"])
            except ValueError:
                likes = None

        desc_text = (desc_el.text or "").strip() if desc_el is not None else ""
        if desc_text:
            descriptions.append(desc_text)

        items.append(
            ContentItem(
                title=title,
                url=f"https://www.youtube.com/watch?v={vid}",
                published_at=pub_el.text.strip() if pub_el is not None and pub_el.text else None,
                views=views,
                likes=likes,
                description_snippet=desc_text[:280],
            )
        )
    return channel_author, items, descriptions


class YouTubeDiscoverer:
    """Discovers YouTube creators via topic searches, channel About pages, and Atom feeds."""

    platform_name = "YouTube"

    def __init__(self, concurrency: int = 8, timeout_seconds: float = 15.0) -> None:
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds

    async def discover(self, preset: NichePreset, limit: int = 55) -> list[InfluencerRaw]:
        """Discover up to `limit` unique YouTube creators for `preset`."""
        async with httpx.AsyncClient(
            headers=YT_HEADERS,
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            stubs: dict[str, tuple[str, str]] = {}
            for idx, query in enumerate(preset.youtube_queries):
                if len(stubs) >= limit + 25:
                    break
                try:
                    encoded = quote_plus(query)
                    # Alternate unfiltered and "This month" (sp=EgIIBA%3D%3D) to balance authority & fresh micro-creators
                    sp_param = "&sp=EgIIBA%3D%3D" if idx % 2 == 1 else ""
                    resp = await client.get(
                        f"https://www.youtube.com/results?search_query={encoded}&gl=US{sp_param}"
                    )
                    if resp.status_code != 200:
                        logger.warning("YouTube search status %s for query '%s'", resp.status_code, query)
                        continue
                    yt_data = extract_yt_initial_data(resp.text)
                    if not yt_data:
                        continue
                    for cid, name in _collect_channel_stubs(yt_data):
                        if cid not in stubs:
                            stubs[cid] = (name, query)
                except Exception as exc:
                    logger.warning("YouTube search failed for '%s': %s", query, exc)

            candidate_items = list(stubs.items())[:limit]
            sem = asyncio.Semaphore(self.concurrency)

            async def fetch_one(cid: str, fallback_name: str, query: str) -> InfluencerRaw | None:
                async with sem:
                    return await self._fetch_channel_profile(client, cid, fallback_name, query)

            results = await asyncio.gather(
                *(fetch_one(cid, meta[0], meta[1]) for cid, meta in candidate_items),
                return_exceptions=False,
            )
            return [r for r in results if r is not None]

    async def _fetch_channel_profile(
        self,
        client: httpx.AsyncClient,
        channel_id: str,
        fallback_name: str,
        discovery_query: str,
    ) -> InfluencerRaw | None:
        about_url = f"https://www.youtube.com/channel/{channel_id}/about"
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

        about_resp, rss_resp = await asyncio.gather(
            client.get(about_url),
            client.get(rss_url),
            return_exceptions=True,
        )

        about_vm: dict[str, Any] = {}
        meta_title = ""
        handle = ""
        canonical_url = f"https://www.youtube.com/channel/{channel_id}"

        if isinstance(about_resp, httpx.Response) and about_resp.status_code == 200:
            yt_data = extract_yt_initial_data(about_resp.text)
            if yt_data:
                vm = _find_first_key(yt_data, "aboutChannelViewModel")
                if isinstance(vm, dict):
                    about_vm = vm
                meta = _find_first_key(yt_data, "channelMetadataRenderer")
                if isinstance(meta, dict):
                    meta_title = (meta.get("title") or "").strip()
                if about_vm.get("canonicalChannelUrl"):
                    canonical_url = str(about_vm["canonicalChannelUrl"])
                display_url = str(about_vm.get("displayCanonicalChannelUrl") or "")
                m_handle = re.search(r"@[\w.\-]+", display_url) or re.search(r"@[\w.\-]+", canonical_url)
                if m_handle:
                    handle = m_handle.group(0)

        rss_author = None
        recent_content: list[ContentItem] = []
        video_descriptions: list[str] = []
        if isinstance(rss_resp, httpx.Response) and rss_resp.status_code == 200:
            rss_author, recent_content, video_descriptions = _parse_rss_feed(rss_resp.text)

        name = meta_title or rss_author or fallback_name or channel_id
        followers = parse_compact_number(about_vm.get("subscriberCountText"))
        bio = (about_vm.get("description") or "").strip()
        country = about_vm.get("country")
        if isinstance(country, str):
            country = country.strip() or None

        external_links = _extract_about_links(about_vm)
        for url in extract_urls(bio):
            if url not in external_links:
                external_links.append(url)

        views_list = [item.views for item in recent_content if item.views is not None]
        likes_list = [item.likes for item in recent_content if item.likes is not None]
        avg_views = round(sum(views_list) / len(views_list), 1) if views_list else None
        avg_likes = round(sum(likes_list) / len(likes_list), 1) if likes_list else None
        likes_hidden = False

        if likes_list and all(val == 0 for val in likes_list) and recent_content:
            likes_hidden = await self._are_video_likes_hidden(
                client, recent_content[0].url, avg_views
            )
            if likes_hidden:
                avg_likes = None
                for item in recent_content:
                    item.likes = None

        engagement_rate = None
        view_to_sub = None
        eng_by_views = None
        if followers and followers > 0:
            if avg_likes is not None:
                engagement_rate = round((avg_likes / followers) * 100.0, 2)
            if avg_views is not None:
                view_to_sub = round((avg_views / followers) * 100.0, 2)
        if avg_views and avg_views > 0 and avg_likes is not None:
            eng_by_views = round((avg_likes / avg_views) * 100.0, 2)

        formula = (
            "Unavailable (public video like counts hidden/disabled by creator)"
            if likes_hidden
            else (
                "Unavailable (no recent public videos in Atom feed)"
                if not recent_content or avg_likes is None
                else "avg_likes / followers * 100 (across up to 12 latest RSS videos)"
            )
        )
        last_post_date = recent_content[0].published_at if recent_content else None

        return InfluencerRaw(
            id=f"yt_{channel_id}",
            name=name,
            handle=handle,
            platform=self.platform_name,
            profile_url=canonical_url,
            followers=followers,
            avg_views=avg_views,
            avg_likes=avg_likes,
            avg_interactions=avg_likes,
            engagement_rate=engagement_rate,
            engagement_by_views_pct=eng_by_views,
            view_to_subscriber_pct=view_to_sub,
            engagement_formula=formula,
            bio=bio,
            country=country,
            discovery_query=discovery_query,
            external_links=external_links,
            recent_content=recent_content,
            last_post_date=last_post_date,
            raw_video_descriptions=video_descriptions,
        )

    @staticmethod
    async def _are_video_likes_hidden(
        client: httpx.AsyncClient,
        video_url: str,
        avg_views: float | None,
    ) -> bool:
        """Distinguish hidden YouTube ratings ('N/A') from actual zero-like counts ('0')."""
        try:
            resp = await client.get(video_url)
            if resp.status_code == 200:
                html = resp.text
                if '"factoidRenderer":{"value":{"simpleText":"N/A"},"label":{"simpleText":"Likes"}' in html:
                    return True
                if '"factoidRenderer":{"value":{"simpleText":"0"},"label":{"simpleText":"Likes"}' in html:
                    return False
        except Exception:
            pass
        return (avg_views or 0.0) >= 100.0
