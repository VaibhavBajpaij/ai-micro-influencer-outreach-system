"""Multi-criteria micro-influencer filtering, scoring, and pass/fail explanation engine."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from outreach_system.config import NichePreset, Settings
from outreach_system.models import FilterDecision, FilterStatus, InfluencerRaw


class InfluencerClassifier:
    """Evaluates raw discovered influencers across reach, engagement, niche, recency, and brand fit."""

    def __init__(self, settings: Settings, preset: NichePreset) -> None:
        self.settings = settings
        self.preset = preset

    def evaluate(self, raw: InfluencerRaw) -> FilterDecision:
        passed: list[str] = []
        failed: list[str] = []

        niche_corpus = self._build_niche_corpus(raw)
        safety_corpus = self._build_safety_corpus(raw)
        niche_score = self._compute_niche_relevance(niche_corpus)
        safety_hits = self._find_brand_safety_flags(safety_corpus)
        days_since_post = self._days_since_last_post(raw.last_post_date)

        # 1. Platform & Profile completeness check
        if raw.platform and raw.profile_url:
            passed.append(f"Platform verified ({raw.platform})")
        else:
            failed.append("Missing platform or profile URL")

        # 2. Follower Count & High-Engagement Equivalence Check
        followers = raw.followers
        follower_ok = False
        high_eng_nano = False

        if followers is None:
            failed.append("Follower count unavailable on public profile")
        elif self.settings.min_followers <= followers <= self.settings.max_followers:
            follower_ok = True
            passed.append(
                f"Followers {followers:,} within micro-influencer range "
                f"({self.settings.min_followers:,}-{self.settings.max_followers:,})"
            )
        elif followers > self.settings.max_followers:
            failed.append(
                f"Followers {followers:,} exceed micro-influencer ceiling "
                f"({self.settings.max_followers:,}; classified as Macro/Mega)"
            )
        elif (
            followers >= self.settings.min_high_engagement_nano_followers
            and raw.engagement_rate is not None
            and raw.engagement_rate >= self.settings.high_engagement_nano_rate_pct
            and (raw.avg_views is None or raw.avg_views >= 1500)
        ):
            follower_ok = True
            high_eng_nano = True
            passed.append(
                f"Followers {followers:,} accepted via high-engagement equivalence "
                f"(engagement={raw.engagement_rate:.2f}% >= {self.settings.high_engagement_nano_rate_pct:.1f}%)"
            )
        else:
            failed.append(
                f"Followers {followers:,} below micro-influencer threshold "
                f"({self.settings.min_followers:,}) without equivalent engagement"
            )

        # 3. Engagement Rate Check (distinguishes unavailable data from actual low engagement)
        eng = raw.engagement_rate
        eng_by_views = raw.engagement_by_views_pct
        view_sub = raw.view_to_subscriber_pct
        avg_views = raw.avg_views

        if eng is None:
            reason = (
                "Engagement data unavailable (public like/reaction metrics hidden or unavailable)"
                if raw.recent_content
                else "Engagement data unavailable (no recent public posts/videos)"
            )
            failed.append(reason)
            if (
                raw.platform == "YouTube"
                and avg_views is not None
                and avg_views < self.settings.min_avg_views_for_youtube
            ):
                failed.append(
                    f"YouTube average views {int(avg_views):,} below minimum ({self.settings.min_avg_views_for_youtube:,})"
                )
        elif eng >= self.settings.min_engagement_rate_pct and (
            raw.platform != "YouTube"
            or avg_views is None
            or avg_views >= self.settings.min_avg_views_for_youtube
        ):
            passed.append(
                f"Engagement rate {eng:.2f}% meets minimum ({self.settings.min_engagement_rate_pct:.2f}%)"
            )
        elif (
            raw.platform == "YouTube"
            and eng >= self.settings.min_engagement_rate_pct
            and avg_views is not None
            and avg_views < self.settings.min_avg_views_for_youtube
        ):
            failed.append(
                f"YouTube average views {int(avg_views):,} below minimum ({self.settings.min_avg_views_for_youtube:,}) despite {eng:.2f}% rate"
            )
        elif (
            raw.platform == "YouTube"
            and eng >= 0.30
            and (avg_views or 0) >= 5_000
            and ((view_sub or 0) >= 35.0 or (eng_by_views or 0) >= 2.0)
        ):
            passed.append(
                f"High-reach YouTube exception: {eng:.2f}% engagement with {int(avg_views or 0):,} avg views "
                f"({view_sub or 0:.1f}% view/sub ratio)"
            )
        else:
            failed.append(
                f"Actual engagement rate {eng:.2f}% below minimum ({self.settings.min_engagement_rate_pct:.2f}%)"
            )
            if (
                raw.platform == "YouTube"
                and avg_views is not None
                and avg_views < self.settings.min_avg_views_for_youtube
            ):
                failed.append(
                    f"YouTube average views {int(avg_views):,} below minimum ({self.settings.min_avg_views_for_youtube:,})"
                )

        # 4. Niche & Content Relevance Check (based on creator bio/videos/posts, never name alone)
        if niche_score >= self.settings.min_niche_relevance_score:
            passed.append(
                f"Content niche relevance {niche_score:.2f} aligns with {self.preset.display_name}"
            )
        else:
            failed.append(
                f"Content niche relevance {niche_score:.2f} below {self.settings.min_niche_relevance_score:.2f} "
                f"for {self.preset.display_name} (bio/recent content check)"
            )

        # 5. Activity Recency Check (unknown/missing timestamps no longer auto-pass)
        if days_since_post is None:
            if self.settings.require_verified_recency:
                failed.append("Activity recency Unknown/Unverified (no timestamped recent posts)")
            else:
                passed.append("Activity recency Unknown/Unverified (accepted by relaxed policy)")
        elif days_since_post <= self.settings.max_days_since_last_post:
            passed.append(f"Active creator (last posted {days_since_post} days ago)")
        else:
            failed.append(
                f"Inactive creator (last posted {days_since_post} days ago > {self.settings.max_days_since_last_post}d limit)"
            )

        # 6. Brand Safety & Brand-Fit Check
        if safety_hits:
            failed.append(f"Brand safety exclusion triggered: {', '.join(safety_hits)}")
        else:
            passed.append("Brand safety check passed (no restricted keywords)")

        # 7. Geography bonus note (informative, non-blocking)
        if raw.country:
            passed.append(f"Geography identified ({raw.country})")

        brand_fit_score = self._compute_brand_fit_score(
            raw=raw,
            niche_score=niche_score,
            days_since_post=days_since_post,
            has_safety_flag=bool(safety_hits),
        )

        # Composite Filter Score (0-100)
        reach_pts = 30.0 if follower_ok else (10.0 if followers and followers > self.settings.max_followers else 5.0)
        eng_pts = min(25.0, (eng or 0.0) * 8.0) if eng is not None else 0.0
        if eng_by_views and eng_by_views >= 2.5:
            eng_pts = max(eng_pts, min(25.0, eng_by_views * 4.0))
        niche_pts = min(25.0, niche_score * 25.0)
        fit_pts = (brand_fit_score / 100.0) * 20.0
        filter_score = round(min(100.0, reach_pts + eng_pts + niche_pts + fit_pts), 1)

        qualified = len(failed) == 0
        tier = self._assign_tier(
            qualified=qualified,
            followers=followers,
            follower_ok=follower_ok,
            engagement_rate=eng,
            filter_score=filter_score,
            high_eng_nano=high_eng_nano,
            niche_score=niche_score,
            safety_hits=safety_hits,
            days_since_post=days_since_post,
        )
        status = FilterStatus.QUALIFIED if qualified else FilterStatus.REJECTED

        if qualified:
            summary = f"PASS ({tier}, score {filter_score}/100): " + "; ".join(passed[:3])
        else:
            summary = f"FAIL ({tier}, score {filter_score}/100): " + "; ".join(failed)

        return FilterDecision(
            status=status,
            tier=tier,
            filter_score=filter_score,
            niche_relevance_score=round(niche_score, 3),
            brand_fit_score=round(brand_fit_score, 1),
            passed_criteria=passed,
            failed_criteria=failed,
            filter_summary=summary,
        )

    def _build_niche_corpus(self, raw: InfluencerRaw) -> str:
        """Build topical corpus strictly from creator bio, titles, and post/video descriptions."""
        titles = " ".join(item.title for item in raw.recent_content)
        snippets = " ".join(item.description_snippet for item in raw.recent_content[:5])
        video_descs = " ".join(d[:400] for d in raw.raw_video_descriptions[:5])
        return f"{raw.bio} {titles} {snippets} {video_descs}".lower()

    def _build_safety_corpus(self, raw: InfluencerRaw) -> str:
        """Include channel name in brand-safety check while keeping niche check content-based."""
        return f"{raw.name} {self._build_niche_corpus(raw)}".lower()

    def _compute_niche_relevance(self, corpus: str) -> float:
        hits = 0
        for kw in self.preset.positive_keywords:
            if re.search(rf"\b{re.escape(kw.lower())}\b", corpus):
                hits += 1
        # 6+ distinct niche keywords => 1.0 relevance
        return min(1.0, hits / 6.0)

    def _find_brand_safety_flags(self, corpus: str) -> list[str]:
        return [
            term
            for term in self.preset.brand_safety_excluded
            if re.search(rf"\b{re.escape(term.lower())}\b", corpus)
        ]

    @staticmethod
    def _days_since_last_post(published_at: str | None) -> int | None:
        if not published_at:
            return None
        try:
            normalized = published_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            delta = datetime.now(UTC) - dt
            return max(0, delta.days)
        except ValueError:
            return None

    def _compute_brand_fit_score(
        self,
        raw: InfluencerRaw,
        niche_score: float,
        days_since_post: int | None,
        has_safety_flag: bool,
    ) -> float:
        if has_safety_flag:
            return 0.0
        score = niche_score * 55.0
        if days_since_post is not None:
            if days_since_post <= 30:
                score += 25.0
            elif days_since_post <= 90:
                score += 18.0
            elif days_since_post <= 120:
                score += 10.0
        else:
            score += 0.0

        # Collaboration readiness signals (bio present, external links, structured content)
        if len(raw.bio) >= 40:
            score += 10.0
        if raw.external_links or "@" in raw.bio:
            score += 10.0
        return min(100.0, score)

    def _assign_tier(
        self,
        qualified: bool,
        followers: int | None,
        follower_ok: bool,
        engagement_rate: float | None,
        filter_score: float,
        high_eng_nano: bool,
        niche_score: float,
        safety_hits: list[str],
        days_since_post: int | None,
    ) -> str:
        if qualified:
            if high_eng_nano:
                return "Tier A-Equiv (High-Engagement Nano)"
            if filter_score >= 75.0:
                return "Tier A (Priority Micro)"
            return "Tier B (Standard Micro)"

        if safety_hits:
            return "Rejected (Brand Safety)"
        if not follower_ok:
            if followers is None:
                return "Rejected (Unavailable Reach)"
            if followers > self.settings.max_followers:
                return "Rejected (Macro / Mega)"
            return "Rejected (Nano / Low Reach)"
        if niche_score < self.settings.min_niche_relevance_score:
            return "Rejected (Off-Niche)"
        if days_since_post is None and self.settings.require_verified_recency:
            return "Rejected (Unverified Activity)"
        if days_since_post is not None and days_since_post > self.settings.max_days_since_last_post:
            return "Rejected (Inactive)"
        if engagement_rate is None:
            return "Rejected (Engagement Unavailable)"
        return "Rejected (Low Engagement)"
