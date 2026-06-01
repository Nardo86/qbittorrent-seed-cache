from __future__ import annotations

from qbittorrent_seed_cache.hotness import HotnessScore
from qbittorrent_seed_cache.selector import (
    TorrentCandidate,
    select_demotions,
    select_displacements,
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


def _displace(cands, **kw):
    defaults = dict(
        now_ts=10_000,
        available_bytes=0,
        promote_min_mb=50,
        min_hot_minutes=0,
        min_cold_minutes=0,
        displacement_factor=2.0,
        max_promotions=1,
        max_evictions=8,
    )
    defaults.update(kw)
    return select_displacements(cands, **defaults)


def test_displace_evicts_lukewarm_squatter_for_hotter_cold() -> None:
    # Cache full (available=0). A lukewarm hot squatter blocks a much hotter
    # cold torrent of the same size.
    cands = [
        _cand("squatter", size_gb=2, per_day_mb=30, tier="hot", age_sec=99999),
        _cand("newcomer", size_gb=2, per_day_mb=900, tier="cold", age_sec=99999),
    ]
    evicted = _displace(cands)
    assert [c.infohash for c in evicted] == ["squatter"]


def test_displace_hysteresis_blocks_similar_heat() -> None:
    # Cold candidate only marginally hotter than the hot one (< 2x) — no swap.
    cands = [
        _cand("hot", size_gb=2, per_day_mb=100, tier="hot", age_sec=99999),
        _cand("cold", size_gb=2, per_day_mb=150, tier="cold", age_sec=99999),
    ]
    assert _displace(cands) == []


def test_displace_evicts_multiple_small_for_one_larger() -> None:
    # A 12GB hot target needs several small victims freed; all are >2x colder.
    cands = [
        _cand("v1", size_gb=4, per_day_mb=5, tier="hot", age_sec=99999),
        _cand("v2", size_gb=4, per_day_mb=10, tier="hot", age_sec=99999),
        _cand("v3", size_gb=4, per_day_mb=20, tier="hot", age_sec=99999),
        _cand("target", size_gb=12, per_day_mb=900, tier="cold", age_sec=99999),
    ]
    evicted = _displace(cands)
    # Coldest-first until 12GB freed: v1+v2+v3 = 12GB.
    assert [c.infohash for c in evicted] == ["v1", "v2", "v3"]


def test_displace_drops_victims_when_target_never_fits() -> None:
    # Target needs 12GB but only one 2GB victim qualifies — never fits, so we
    # must not evict for nothing.
    cands = [
        _cand("v1", size_gb=2, per_day_mb=5, tier="hot", age_sec=99999),
        _cand("safe", size_gb=10, per_day_mb=800, tier="hot", age_sec=99999),
        _cand("target", size_gb=12, per_day_mb=900, tier="cold", age_sec=99999),
    ]
    # 'safe' is not >2x colder than target (900 vs 800), so it's not evictable
    # for it; only v1 (2GB) qualifies, which can't free 12GB → evict nothing.
    assert _displace(cands) == []


def test_displace_respects_max_evictions() -> None:
    cands = [
        _cand(f"v{i}", size_gb=4, per_day_mb=5, tier="hot", age_sec=99999)
        for i in range(5)
    ]
    cands.append(_cand("target", size_gb=20, per_day_mb=900, tier="cold", age_sec=99999))
    # 20GB target needs 5x4GB victims, but capped at 2 evictions → can't fit →
    # nothing committed.
    assert _displace(cands, max_evictions=2) == []


def test_displace_skips_fresh_hot() -> None:
    cands = [
        _cand("fresh", size_gb=2, per_day_mb=10, tier="hot", age_sec=10),
        _cand("cold", size_gb=2, per_day_mb=900, tier="cold", age_sec=99999),
    ]
    assert _displace(cands, min_hot_minutes=60) == []


def test_displace_noop_when_headroom_already_fits() -> None:
    cands = [
        _cand("squatter", size_gb=2, per_day_mb=30, tier="hot", age_sec=99999),
        _cand("cold", size_gb=2, per_day_mb=900, tier="cold", age_sec=99999),
    ]
    # Plenty of headroom → promote step handles it, no eviction needed.
    assert _displace(cands, available_bytes=10 * GB) == []


def test_displace_respects_max_promotions() -> None:
    cands = [
        _cand("v1", size_gb=2, per_day_mb=5, tier="hot", age_sec=99999),
        _cand("v2", size_gb=2, per_day_mb=5, tier="hot", age_sec=99999),
        _cand("c1", size_gb=2, per_day_mb=900, tier="cold", age_sec=99999),
        _cand("c2", size_gb=2, per_day_mb=900, tier="cold", age_sec=99999),
    ]
    # Only room freed for 1 promotion → only 1 victim evicted.
    evicted = _displace(cands, max_promotions=1)
    assert len(evicted) == 1


def test_displace_density_beats_absolute_rate() -> None:
    # 'bigshot' has >2x the ABSOLUTE upload of 'victim' (absolute-rate
    # hysteresis would evict the victim), but far worse per-GB density: caching
    # it would waste the slot. Density-aware displacement keeps the small dense
    # torrent and refuses the swap.
    cands = [
        _cand("victim", size_gb=2, per_day_mb=300, tier="hot", age_sec=99999),
        _cand("bigshot", size_gb=40, per_day_mb=700, tier="cold", age_sec=99999),
    ]
    # 38GB headroom + evicting the 2GB victim would fit 'bigshot' by size, but
    # density(bigshot)=17.5 < density(victim)=150 → no eviction.
    assert _displace(cands, available_bytes=38 * GB) == []


def test_displace_evicts_low_density_pack_for_dense_releases() -> None:
    # A big low-density pack already hot; two small dense cold releases want in.
    # Evicting the pack frees room for both and serves far more per GB.
    cands = [
        _cand("pack", size_gb=40, per_day_mb=600, tier="hot", age_sec=99999),
        _cand("dense1", size_gb=4, per_day_mb=900, tier="cold", age_sec=99999),
        _cand("dense2", size_gb=4, per_day_mb=800, tier="cold", age_sec=99999),
    ]
    evicted = _displace(cands, available_bytes=0, max_promotions=2)
    assert [c.infohash for c in evicted] == ["pack"]


def test_displace_excludes_oversized_target() -> None:
    cands = [
        _cand("v1", size_gb=2, per_day_mb=5, tier="hot", age_sec=99999),
        _cand("huge", size_gb=300, per_day_mb=900, tier="cold", age_sec=99999),
    ]
    # Size cap excludes the huge filmography as a promotion target → no eviction.
    assert _displace(cands, max_size_bytes=int(50 * GB)) == []
