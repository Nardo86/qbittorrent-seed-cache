"""Pure candidate-selection logic.

Kept free of I/O so it's trivially unit-testable. The daemon glues it to
the StateStore and the QbitClient.

Thresholds are configured as MB/day; we compare against the per-day rate
from HotnessScore.upload_bytes_per_day (bytes / second → MB / day).

Candidates are *logical* (per-infohash), not per (instance, infohash). When
the same infohash is seeded by multiple qB instances, hotness scores are
summed by the caller and the candidate carries the list of instances for
traceability.
"""

from __future__ import annotations

from dataclasses import dataclass

from .hotness import HotnessScore


@dataclass(frozen=True, slots=True)
class TorrentCandidate:
    """Aggregated view of one *logical* torrent (infohash) for selection."""

    infohash: str
    size_bytes: int
    score: HotnessScore
    # 'cold', 'hot', or None if we've never tracked it.
    current_tier: str | None
    # Unix ts when current_tier was assigned (0 if unknown).
    tier_since_ts: int
    # qB instance names hosting this infohash. Informational; the selector
    # does not use it.
    instances: tuple[str, ...] = ()


def _per_day_mb(score: HotnessScore) -> float:
    return score.upload_bytes_per_day / (1024 * 1024)


def _density(c: TorrentCandidate) -> float:
    """Upload bytes/day served per byte of SSD the torrent would occupy.

    The cache is size-constrained, so the right thing to maximise is upload
    *per GB of cache*, not absolute upload. A 60 GB filmography uploading
    30 GB/day (density 0.5) is a far worse cache occupant than a 2 GB release
    uploading 5 GB/day (density 2.5): caching the former would evict several of
    the latter and serve *less* from the SSD overall. Selection and eviction
    therefore rank by this ratio. A zero/unknown size sorts as density 0.
    """
    if c.size_bytes <= 0:
        return 0.0
    return c.score.upload_bytes_per_day / c.size_bytes


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
    max_size_bytes: int | None = None,
) -> list[TorrentCandidate]:
    """Return cold torrents to promote, densest first, subject to quota.

    A torrent is promoted when:
    - It is currently cold (or never tracked).
    - It has been cold for at least `min_cold_minutes` (skipped if never tracked).
    - Its upload rate is above `promote_min_mb` per day (an absolute floor, so
      we don't cache a tiny torrent that's only "dense" by ratio).
    - Its size is at or below `max_size_bytes` (if set).

    We pick greedily by *density* (upload/day per byte) DESC, fitting up to
    `available_bytes` and at most `max_concurrent` torrents per tick (to bound
    HDD parallelism). Density, not absolute rate, maximises the upload served
    per GB of a size-constrained cache.
    """
    min_age_sec = min_cold_minutes * 60

    def cold_enough(c: TorrentCandidate) -> bool:
        if c.current_tier is None:
            return True
        if c.current_tier != "cold":
            return False
        return (now_ts - c.tier_since_ts) >= min_age_sec

    def under_size_cap(c: TorrentCandidate) -> bool:
        return max_size_bytes is None or c.size_bytes <= max_size_bytes

    eligible = [
        c
        for c in candidates
        if cold_enough(c)
        and under_size_cap(c)
        and _per_day_mb(c.score) >= promote_min_mb
    ]
    eligible.sort(key=_density, reverse=True)

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


def select_displacements(
    candidates: list[TorrentCandidate],
    *,
    now_ts: int,
    available_bytes: int,
    promote_min_mb: float,
    min_hot_minutes: int,
    min_cold_minutes: int,
    displacement_factor: float,
    max_promotions: int,
    max_evictions: int,
    max_size_bytes: int | None = None,
) -> list[TorrentCandidate]:
    """Return hot torrents to evict so denser cold ones can be promoted.

    `select_demotions` only retires torrents that have themselves gone cold, so
    a cache full of lukewarm-but-not-dead torrents has no headroom and never
    lets a more valuable cold torrent in. This evicts the *least dense* hot
    torrents so the freed space (on top of `available_bytes`) lets the densest
    cold candidates be promoted by the subsequent promote step.

    Value is measured by **density** — upload/day per byte of SSD occupied —
    not absolute rate, so a huge low-density pack can't push out several small
    high-density releases that together serve far more from the cache.

    Hysteresis: a hot torrent ``H`` is evicted only to make room for a cold
    candidate ``C`` with ``density(C) >= density(H) * displacement_factor``.
    With ``displacement_factor > 1`` two torrents of similar density never swap
    slots tick after tick.

    Bounds: room is freed for at most ``max_promotions`` cold candidates (it is
    the promote step that bounds HDD-read parallelism), evicting at most
    ``max_evictions`` hot torrents in total. An eviction is only committed once
    it actually lets a candidate fit — victims that free space for a target too
    large to ever fit are dropped, so we never evict for nothing.

    Returns the hot candidates to demote (least dense first); [] when no
    displacement is worthwhile.
    """
    min_cold_sec = min_cold_minutes * 60
    min_hot_sec = min_hot_minutes * 60

    def cold_enough(c: TorrentCandidate) -> bool:
        if c.current_tier is None:
            return True
        if c.current_tier != "cold":
            return False
        return (now_ts - c.tier_since_ts) >= min_cold_sec

    def under_size_cap(c: TorrentCandidate) -> bool:
        return max_size_bytes is None or c.size_bytes <= max_size_bytes

    wanted = sorted(
        (
            c
            for c in candidates
            if cold_enough(c)
            and under_size_cap(c)
            and _per_day_mb(c.score) >= promote_min_mb
        ),
        key=_density,
        reverse=True,
    )
    evictable = sorted(
        (
            c
            for c in candidates
            if c.current_tier == "hot"
            and (now_ts - c.tier_since_ts) >= min_hot_sec
        ),
        key=_density,
    )
    if not wanted or not evictable:
        return []

    committed: list[TorrentCandidate] = []
    freed = max(0, available_bytes)
    ei = 0  # next (least dense) evictable not yet committed
    promoted = 0
    w = 0
    while w < len(wanted) and promoted < max_promotions and len(committed) < max_evictions:
        target = wanted[w]
        if target.size_bytes <= freed:
            # Fits in space we already have (existing headroom or carry-over
            # from a previous target's evictions) — no eviction needed.
            freed -= target.size_bytes
            w += 1
            promoted += 1
            continue
        # Tentatively evict the coldest hot torrents (factor-gated) until the
        # target fits. Don't commit until we know it actually fits: a target
        # too large to free room for must not cost us evictions for nothing.
        trial: list[TorrentCandidate] = []
        trial_freed = freed
        j = ei
        while (
            target.size_bytes > trial_freed
            and j < len(evictable)
            and len(committed) + len(trial) < max_evictions
        ):
            h = evictable[j]
            if _density(target) < _density(h) * displacement_factor:
                break  # least-dense available victim still beats target → give up
            trial.append(h)
            trial_freed += h.size_bytes
            j += 1
        if target.size_bytes <= trial_freed:
            committed.extend(trial)
            freed = trial_freed - target.size_bytes
            ei = j
            promoted += 1
        # Whether it fit or not, move on; if it didn't, the trial evictions are
        # discarded (not committed) and the next, smaller target is tried.
        w += 1
    return committed
