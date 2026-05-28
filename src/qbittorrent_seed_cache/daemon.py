"""Main loop: poll → score → demote → promote.

A tick:
  1. For each instance: log in to qB, fetch torrents+files, resolve their
     host-side TorrentLayouts, record an upload snapshot in SQLite.
  2. Score each resolved torrent against the rolling window.
  3. Select demotions (hot → cold) and apply them — frees SSD bytes.
  4. Recompute available headroom.
  5. Select promotions (cold → hot) within headroom, apply sequentially
     (bounded by max_concurrent_promotions, defending the HDD parallelism).
  6. Update the tier table for every applied transition.

Crash recovery: tier records survive restarts. If a transition is
interrupted mid-way, the on-disk symlink target is the authoritative
state — the next tick re-derives the tier from the SSD usage and converges.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
import time

import structlog

from .config import Config, InstanceConfig
from .hotness import score as score_history
from .mover import TorrentLayout, demote, promote
from .qbit_client import QbitClient, TorrentInfo
from .resolver import ResolvedTorrent, resolve
from .selector import TorrentCandidate, select_demotions, select_promotions
from .state import StateStore

log = structlog.get_logger(__name__)


async def _collect_instance(
    instance: InstanceConfig, config: Config
) -> tuple[list[TorrentInfo], dict[str, ResolvedTorrent]]:
    """Talk to one qB instance: return torrents + resolved layouts by infohash."""
    async with QbitClient(
        name=instance.name,
        url=instance.url,
        username=instance.username,
        password=instance.password.get_secret_value(),
    ) as client:
        torrents = await client.torrents()
        resolved: dict[str, ResolvedTorrent] = {}
        for t in torrents:
            files = await client.torrent_files(t.hash)
            r = resolve(
                instance=instance.name,
                torrent=t,
                files=files,
                ssd_cache_dir=config.ssd_cache_dir,
                path_map=instance.path_map,
                managed_paths=config.managed_paths,
            )
            if r is not None:
                resolved[t.hash] = r
        return torrents, resolved


def _build_candidates(
    instance_name: str,
    torrents: list[TorrentInfo],
    resolved: dict[str, ResolvedTorrent],
    store: StateStore,
    *,
    now_ts: int,
    window_seconds: int,
) -> list[TorrentCandidate]:
    candidates: list[TorrentCandidate] = []
    cutoff = now_ts - window_seconds
    for t in torrents:
        r = resolved.get(t.hash)
        if r is None:
            continue
        history = store.history(instance=instance_name, infohash=t.hash, since_ts=cutoff)
        hs = score_history(history, window_seconds=window_seconds)
        tier_row = store.get_tier(instance=instance_name, infohash=t.hash)
        if tier_row is not None:
            tier, since_ts = tier_row
        else:
            tier, since_ts = None, 0
        candidates.append(
            TorrentCandidate(
                instance=instance_name,
                infohash=t.hash,
                size_bytes=r.total_bytes,
                score=hs,
                current_tier=tier,
                tier_since_ts=since_ts,
            )
        )
    return candidates


def _bootstrap_tier(
    candidates: list[TorrentCandidate],
    resolved: dict[str, ResolvedTorrent],
    store: StateStore,
    *,
    now_ts: int,
) -> None:
    """For any candidate without a tier record, infer it from the symlink state."""
    for c in candidates:
        if c.current_tier is not None:
            continue
        r = resolved[c.infohash]
        tier = "hot" if r.is_hot_on_ssd else "cold"
        ssd_bytes = r.total_bytes if tier == "hot" else 0
        store.set_tier(
            instance=c.instance,
            infohash=c.infohash,
            tier=tier,
            since_ts=now_ts,
            ssd_bytes=ssd_bytes,
        )


def _free_ssd_bytes(config: Config, store: StateStore) -> int:
    """Available bytes for new promotions: min(quota_headroom, fs_free - min_free)."""
    used = store.hot_total_bytes()
    quota_b = int(config.quota_gb * 1024**3)
    min_free_b = int(config.min_free_gb * 1024**3)
    free = shutil.disk_usage(config.ssd_cache_dir).free
    return max(0, min(quota_b - used, free - min_free_b))


def _apply_demotion(
    layouts: list[TorrentLayout], *, dry_run: bool
) -> int:
    freed = 0
    for layout in layouts:
        freed += demote(layout, dry_run=dry_run)
    return freed


def _apply_promotion(
    layouts: list[TorrentLayout], *, dry_run: bool
) -> int:
    copied = 0
    for layout in layouts:
        copied += promote(layout, dry_run=dry_run)
    return copied


async def _tick(config: Config, store: StateStore) -> None:
    now = int(time.time())

    # 1. Poll every instance in parallel.
    instance_results = await asyncio.gather(
        *(_collect_instance(i, config) for i in config.instances),
        return_exceptions=True,
    )

    all_candidates: list[TorrentCandidate] = []
    resolved_by_key: dict[tuple[str, str], ResolvedTorrent] = {}

    window_seconds = config.hotness.window_days * 86_400

    for instance, result in zip(config.instances, instance_results, strict=True):
        if isinstance(result, BaseException):
            log.error("instance.poll_failed", instance=instance.name, error=str(result))
            continue
        torrents, resolved = result

        for t in torrents:
            await asyncio.to_thread(
                store.record,
                instance=instance.name,
                infohash=t.hash,
                ts=now,
                uploaded_session=t.uploaded_session,
                upspeed=t.upspeed,
            )

        instance_candidates = _build_candidates(
            instance.name, torrents, resolved, store,
            now_ts=now, window_seconds=window_seconds,
        )
        _bootstrap_tier(instance_candidates, resolved, store, now_ts=now)

        all_candidates.extend(instance_candidates)
        for h, r in resolved.items():
            resolved_by_key[(instance.name, h)] = r

        log.info(
            "tick.snapshot",
            instance=instance.name,
            torrents=len(torrents),
            resolved=len(resolved),
        )

    # 2. Prune old snapshots.
    pruned = await asyncio.to_thread(store.prune, before_ts=now - window_seconds)
    if pruned:
        log.info("tick.pruned", rows=pruned)

    # 3. Demote first (frees quota).
    demotions = select_demotions(
        all_candidates,
        now_ts=now,
        demote_max_mb=config.hotness.demote_max_upload_mb,
        min_hot_minutes=config.hotness.min_hot_minutes,
    )
    for c in demotions:
        r = resolved_by_key[(c.instance, c.infohash)]
        try:
            await asyncio.to_thread(_apply_demotion, r.layouts, dry_run=config.dry_run)
        except Exception:
            log.exception("demote.failed", instance=c.instance, infohash=c.infohash)
            continue
        if not config.dry_run:
            store.set_tier(
                instance=c.instance, infohash=c.infohash,
                tier="cold", since_ts=now, ssd_bytes=0,
            )
    if demotions:
        log.info("tick.demoted", count=len(demotions))

    # 4. Recompute headroom.
    available = await asyncio.to_thread(_free_ssd_bytes, config, store)
    log.info("tick.headroom_bytes", bytes=available)

    # 5. Promote within headroom.
    promotions = select_promotions(
        all_candidates,
        now_ts=now,
        promote_min_mb=config.hotness.promote_min_upload_mb,
        min_cold_minutes=config.hotness.min_cold_minutes,
        available_bytes=available,
        max_concurrent=config.max_concurrent_promotions,
    )
    for c in promotions:
        r = resolved_by_key[(c.instance, c.infohash)]
        try:
            await asyncio.to_thread(_apply_promotion, r.layouts, dry_run=config.dry_run)
        except Exception:
            log.exception("promote.failed", instance=c.instance, infohash=c.infohash)
            continue
        if not config.dry_run:
            store.set_tier(
                instance=c.instance, infohash=c.infohash,
                tier="hot", since_ts=now, ssd_bytes=c.size_bytes,
            )
    if promotions:
        log.info("tick.promoted", count=len(promotions))


async def run_daemon(config: Config) -> None:
    log.info(
        "daemon.start",
        instances=[i.name for i in config.instances],
        poll_interval_sec=config.poll_interval_sec,
        dry_run=config.dry_run,
    )

    # Ensure cache dir + marker file exist (mover is rw on the SSD).
    config.ssd_cache_dir.mkdir(parents=True, exist_ok=True)
    marker = config.ssd_cache_dir / ".ssd-mount-ok"
    if not marker.exists():
        marker.touch()

    store = StateStore(config.state_db)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            try:
                await _tick(config, store)
            except Exception:
                log.exception("tick.failed")

            try:
                await asyncio.wait_for(stop.wait(), timeout=config.poll_interval_sec)
            except asyncio.TimeoutError:
                pass
    finally:
        store.close()
        log.info("daemon.stop")
