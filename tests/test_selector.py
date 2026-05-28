from __future__ import annotations

from qbittorrent_seed_cache.hotness import HotnessScore
from qbittorrent_seed_cache.selector import (
    TorrentCandidate,
    select_demotions,
    select_promotions,
)

MB = 1024 * 1024
GB = 1024 * MB


def _cand(
    name: str,
    *,
    size_gb: float = 1.0,
    per_day_mb: float = 0.0,
    tier: str | None = None,
    age_sec: int = 0,
    now: int = 10_000,
) -> TorrentCandidate:
    return TorrentCandidate(
        infohash=name,
        size_bytes=int(size_gb * GB),
        score=HotnessScore(
            upload_bytes_in_window=int(per_day_mb * MB * 14),
            upload_bytes_per_day=per_day_mb * MB,
            last_activity_ts=now,
        ),
        current_tier=tier,
        tier_since_ts=now - age_sec,
        instances=("qb",),
    )


def test_promote_selects_hottest_first_within_quota() -> None:
    cands = [
        _cand("hot-big", size_gb=80, per_day_mb=200, tier="cold", age_sec=99999),
        _cand("hot-small", size_gb=10, per_day_mb=300, tier="cold", age_sec=99999),
        _cand("cold", size_gb=10, per_day_mb=1, tier="cold", age_sec=99999),
    ]
    picked = select_promotions(
        cands,
        now_ts=10_000,
        promote_min_mb=50,
        min_cold_minutes=0,
        available_bytes=50 * GB,
        max_concurrent=10,
    )
    # hot-small fits and is hotter; hot-big is hotter but doesn't fit at 80GB.
    picked_names = [c.infohash for c in picked]
    assert picked_names == ["hot-small"]


def test_promote_respects_max_concurrent() -> None:
    cands = [
        _cand(f"t{i}", size_gb=1, per_day_mb=100 + i, tier="cold", age_sec=99999)
        for i in range(5)
    ]
    picked = select_promotions(
        cands,
        now_ts=10_000,
        promote_min_mb=50,
        min_cold_minutes=0,
        available_bytes=100 * GB,
        max_concurrent=2,
    )
    assert len(picked) == 2
    # Hottest first (highest per_day_mb).
    assert picked[0].infohash == "t4"
    assert picked[1].infohash == "t3"


def test_promote_skips_too_recently_cold() -> None:
    c = _cand("warm", size_gb=1, per_day_mb=200, tier="cold", age_sec=10)
    picked = select_promotions(
        [c],
        now_ts=10_000,
        promote_min_mb=50,
        min_cold_minutes=60,
        available_bytes=10 * GB,
        max_concurrent=10,
    )
    assert picked == []


def test_promote_allows_untracked() -> None:
    c = _cand("new", size_gb=1, per_day_mb=200, tier=None)
    picked = select_promotions(
        [c],
        now_ts=10_000,
        promote_min_mb=50,
        min_cold_minutes=60,
        available_bytes=10 * GB,
        max_concurrent=10,
    )
    assert [p.infohash for p in picked] == ["new"]


def test_demote_picks_coldest_first() -> None:
    cands = [
        _cand("a", per_day_mb=4, tier="hot", age_sec=99999),
        _cand("b", per_day_mb=1, tier="hot", age_sec=99999),
        _cand("c", per_day_mb=2, tier="hot", age_sec=99999),
    ]
    picked = select_demotions(
        cands, now_ts=10_000, demote_max_mb=5, min_hot_minutes=0
    )
    assert [p.infohash for p in picked] == ["b", "c", "a"]


def test_demote_requires_min_hot_minutes() -> None:
    c = _cand("fresh", per_day_mb=1, tier="hot", age_sec=10)
    picked = select_demotions(
        [c], now_ts=10_000, demote_max_mb=5, min_hot_minutes=60
    )
    assert picked == []


def test_promote_excludes_oversized_torrents() -> None:
    cands = [
        _cand("huge", size_gb=300, per_day_mb=500, tier="cold", age_sec=99999),
        _cand("ok", size_gb=20, per_day_mb=100, tier="cold", age_sec=99999),
    ]
    picked = select_promotions(
        cands,
        now_ts=10_000,
        promote_min_mb=50,
        min_cold_minutes=0,
        available_bytes=500 * GB,
        max_concurrent=10,
        max_size_bytes=int(50 * GB),
    )
    # 'huge' is excluded by the size cap even though it's hotter and would fit.
    assert [c.infohash for c in picked] == ["ok"]


def test_promote_no_cap_when_max_size_none() -> None:
    cands = [
        _cand("huge", size_gb=300, per_day_mb=500, tier="cold", age_sec=99999),
    ]
    picked = select_promotions(
        cands,
        now_ts=10_000,
        promote_min_mb=50,
        min_cold_minutes=0,
        available_bytes=500 * GB,
        max_concurrent=10,
        max_size_bytes=None,
    )
    assert [c.infohash for c in picked] == ["huge"]


def test_demote_ignores_cold() -> None:
    c = _cand("cold", per_day_mb=1, tier="cold", age_sec=99999)
    picked = select_demotions(
        [c], now_ts=10_000, demote_max_mb=5, min_hot_minutes=0
    )
    assert picked == []
