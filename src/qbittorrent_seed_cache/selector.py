"""Pure candidate-selection logic.

Kept free of I/O so it's trivially unit-testable. The daemon glues it to
the StateStore and the QbitClient.

Thresholds are configured as MB/day; we compare against the per-day rate
from HotnessScore.upload_bytes_per_day (bytes/sec * 86400 / 1024**2 ≈ MB/day).
"""

from __future__ import annotations

from dataclasses import dataclass

from .hotness import HotnessScore


@dataclass(frozen=True, slots=True)
class TorrentCandidate:
    """Aggregated view of one torrent for selection purposes."""

    instance: str
    infohash: str
    size_bytes: int
    score: HotnessScore
    # 'cold', 'hot', or None if we've never tracked it.
    current_tier: str | None
    # Unix ts when current_tier was assigned (0 if unknown).
    tier_since_ts: int


def _per_day_mb(score: HotnessScore) -> float:
    return score.upload_bytes_per_day / (1024 * 1024)


def select_demotions(
    candidates: list[TorrentCandidate],
    *,
    now_ts: int,
    demote_max_mb: float,
    min_hot_minutes: int,
) -> list[TorrentCandidate]:
    """Return hot torrents that should be demoted, coldest first.

    A torrent is demoted when:
    - It is currently hot.
    - It has been hot for at least `min_hot_minutes`.
    - Its upload rate is below `demote_max_mb` per day.
    """
    min_age_sec = min_hot_minutes * 60
    eligible = [
        c
        for c in candidates
        if c.current_tier == "hot"
        and (now_ts - c.tier_since_ts) >= min_age_sec
        and _per_day_mb(c.score) <= demote_max_mb
    ]
    eligible.sort(key=lambda c: _per_day_mb(c.score))
    return eligible


def select_promotions(
    candidates: list[TorrentCandidate],
    *,
    now_ts: int,
    promote_min_mb: float,
    min_cold_minutes: int,
    available_bytes: int,
    max_concurrent: int,
) -> list[TorrentCandidate]:
    """Return cold torrents to promote, hottest first, subject to quota.

    A torrent is promoted when:
    - It is currently cold (or never tracked).
    - It has been cold for at least `min_cold_minutes` (skipped if never tracked).
    - Its upload rate is above `promote_min_mb` per day.

    We pick greedily by hotness DESC, fitting up to `available_bytes` and at
    most `max_concurrent` torrents per tick (to bound HDD parallelism).
    """
    min_age_sec = min_cold_minutes * 60

    def cold_enough(c: TorrentCandidate) -> bool:
        if c.current_tier is None:
            return True
        if c.current_tier != "cold":
            return False
        return (now_ts - c.tier_since_ts) >= min_age_sec

    eligible = [c for c in candidates if cold_enough(c) and _per_day_mb(c.score) >= promote_min_mb]
    eligible.sort(key=lambda c: _per_day_mb(c.score), reverse=True)

    picked: list[TorrentCandidate] = []
    remaining = max(0, available_bytes)
    for c in eligible:
        if len(picked) >= max_concurrent:
            break
        if c.size_bytes > remaining:
            continue
        picked.append(c)
        remaining -= c.size_bytes
    return picked
