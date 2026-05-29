"""Main loop: poll → aggregate by infohash → score → demote → promote.

Per-tick flow:
  1. Load persisted `bulk_targets` for every hot torrent (so the resolver
     can recover the bulk path of a torrent whose symlink now points
     into the SSD cache).
  2. For each qB instance: log in, fetch torrents+files, resolve host paths,
     persist a snapshot. Per-instance metrics are stored as-is.
  3. Aggregate per-instance ResolvedTorrents into LogicalTorrents keyed by
     infohash. The SSD copy is shared across instances; symlinks in each
     instance are retargeted in lockstep.
  4. Score hotness per (instance, infohash) and sum into a per-infohash
     score. The `instances` list on each candidate is informational.
  5. Bootstrap tier rows for previously-unknown infohashes from the
     current symlink state (is_hot_on_ssd).
  6. Cleanup orphans: infohashes with tier='hot' that no longer exist in
     any instance (probably removed from qB) — rm their SSD dir, drop tier.
  7. Apply demotions first (frees SSD bytes).
  7b. Reclaim SSD dirs that are neither hot nor referenced by a live symlink,
     and (re)evaluate the anomaly marker from the live view. Steps 6 and 7b
     are skipped if any instance poll failed (the live set would be partial).
  8. Recompute available headroom.
  9. Apply promotions within headroom (greedy by hotness, capped by
     max_concurrent_promotions). On success, persist the bulk_targets
     map alongside the tier row.

Crash recovery: tier rows survive restarts. If a transition is interrupted
mid-way, the on-disk symlinks + SSD content are authoritative; the next
tick re-derives the tier from is_hot_on_ssd and converges.

Startup reconciliation: before the first tick, `reconcile_startup` aligns
the DB with the filesystem using the per-torrent `.qbsc-meta.json` sidecars
(see `recovery` and `reconcile`). This makes the cache survive a lost or
replaced DB without losing track of already-promoted content.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import time

import structlog

from . import recovery
from .config import Config, InstanceConfig
from .hotness import HotnessScore
from .hotness import score as score_history
from .mover import bulk_targets_of, demote, promote
from .qbit_client import QbitClient
from .reconcile import reconcile_startup
from .resolver import LogicalTorrent, ResolvedTorrent, aggregate, references_ssd, resolve
from .selector import TorrentCandidate, select_demotions, select_promotions
from .state import StateStore
from .symlinks import remove_tree

log = structlog.get_logger(__name__)


async def _collect_instance(
    instance: InstanceConfig,
    config: Config,
    hot_bulk_maps: dict[str, dict[str, str]],
) -> tuple[list[tuple[str, int, int]], list[ResolvedTorrent], set[str]]:
    """Poll one qB instance.

    Returns:
      - snapshots: list of (infohash, uploaded_session, upspeed) tuples to record.
      - resolved: list of ResolvedTorrent for torrents that follow the symlink
        convention.
      - ssd_referenced: infohashes whose live symlinks currently resolve into
        the SSD cache (whether or not their bulk origin could be recovered).
        Used to protect referenced cache dirs from orphan reclamation.

    `hot_bulk_maps[infohash]` is the persisted {link_path: bulk_path} map for
    torrents already promoted to SSD; passed through to resolve() so that the
    bulk file can be recovered even when readlink() now points into the SSD.
    """
    snapshots: list[tuple[str, int, int]] = []
    resolved_list: list[ResolvedTorrent] = []
    ssd_referenced: set[str] = set()
    async with QbitClient(
        name=instance.name,
        url=instance.url,
        username=instance.username,
        password=instance.password.get_secret_value(),
    ) as client:
        torrents = await client.torrents()
        for t in torrents:
            snapshots.append((t.hash, t.uploaded_session, t.upspeed))
            files = await client.torrent_files(t.hash)
            r = resolve(
                instance=instance.name,
                torrent=t,
                files=files,
                ssd_cache_dir=config.ssd_cache_dir,
                path_map=instance.path_map,
                managed_paths=config.managed_paths,
                hot_bulk_map=hot_bulk_maps.get(t.hash),
            )
            if r is not None:
                resolved_list.append(r)
            if references_ssd(
                torrent=t,
                files=files,
                ssd_cache_dir=config.ssd_cache_dir,
                path_map=instance.path_map,
            ):
                ssd_referenced.add(t.hash)
    return snapshots, resolved_list, ssd_referenced


def _aggregate_score(
    instances: tuple[str, ...],
    infohash: str,
    store: StateStore,
    *,
    now_ts: int,
    window_seconds: int,
) -> HotnessScore:
    """Sum per-instance hotness into a logical-torrent score."""
    cutoff = now_ts - window_seconds
    total_window = 0
    total_per_day = 0.0
    last_activity = 0
    for name in instances:
        history = store.history(instance=name, infohash=infohash, since_ts=cutoff)
        s = score_history(history, window_seconds=window_seconds)
        total_window += s.upload_bytes_in_window
        total_per_day += s.upload_bytes_per_day
        last_activity = max(last_activity, s.last_activity_ts)
    return HotnessScore(
        upload_bytes_in_window=total_window,
        upload_bytes_per_day=total_per_day,
        last_activity_ts=last_activity,
    )


def _build_candidates(
    logical: dict[str, LogicalTorrent],
    store: StateStore,
    *,
    now_ts: int,
    window_seconds: int,
) -> list[TorrentCandidate]:
    out: list[TorrentCandidate] = []
    for infohash, lt in logical.items():
        score = _aggregate_score(
            lt.instances, infohash, store,
            now_ts=now_ts, window_seconds=window_seconds,
        )
        tier_row = store.get_tier(infohash=infohash)
        if tier_row is None:
            current_tier: str | None = None
            since_ts = 0
        else:
            current_tier = tier_row.tier
            since_ts = tier_row.since_ts
        out.append(
            TorrentCandidate(
                infohash=infohash,
                size_bytes=lt.size_bytes,
                score=score,
                current_tier=current_tier,
                tier_since_ts=since_ts,
                instances=lt.instances,
            )
        )
    return out


def _bootstrap_tier(
    candidates: list[TorrentCandidate],
    logical: dict[str, LogicalTorrent],
    store: StateStore,
    *,
    now_ts: int,
) -> None:
    """Infer tier from current symlink state for any new infohash."""
    for c in candidates:
        if c.current_tier is not None:
            continue
        lt = logical[c.infohash]
        tier = "hot" if lt.is_hot_on_ssd else "cold"
        ssd_bytes = lt.size_bytes if tier == "hot" else 0
        bulk_targets = (
            {str(layout.link): str(layout.bulk_target) for layout in lt.layouts}
            if tier == "hot"
            else None
        )
        store.set_tier(
            infohash=c.infohash,
            tier=tier,
            since_ts=now_ts,
            ssd_bytes=ssd_bytes,
            bulk_targets=bulk_targets,
        )


def _cleanup_orphans(
    live_infohashes: set[str], config: Config, store: StateStore
) -> int:
    """Drop SSD content + tier row for hot infohashes no longer in any qB instance."""
    freed_count = 0
    for ih in store.hot_infohashes():
        if ih in live_infohashes:
            continue
        ssd_dir = config.ssd_cache_dir / ih
        log.info("orphan.cleanup", infohash=ih, ssd_dir=str(ssd_dir), dry_run=config.dry_run)
        if not config.dry_run:
            remove_tree(ssd_dir)
            store.delete_tier(infohash=ih)
        freed_count += 1
    return freed_count


def _cleanup_fs_orphans(
    config: Config, store: StateStore, ssd_referenced: set[str]
) -> int:
    """Reclaim `<ssd_cache_dir>/<infohash>/` dirs that nothing uses.

    A dir is in use iff its infohash is hot in the DB OR a live qB symlink
    currently resolves into it (`ssd_referenced`). Anything else is an orphan
    — typically a demote that crashed after retargeting the symlink to bulk
    but before removing the SSD copy, or a torrent removed from qB. Because we
    require it to be *unreferenced by any live symlink*, deleting it cannot
    dangle a seed. Reclaiming here (before the headroom recompute) returns the
    space to the promotion budget.
    """
    in_use = set(store.hot_infohashes()) | ssd_referenced
    reclaimed = 0
    for infohash, ssd_dir in recovery.iter_ssd_infohash_dirs(config.ssd_cache_dir):
        if infohash in in_use:
            continue
        log.info(
            "orphan.fs_reclaim",
            infohash=infohash,
            ssd_dir=str(ssd_dir),
            payload=recovery.ssd_dir_has_payload(ssd_dir),
            dry_run=config.dry_run,
        )
        if not config.dry_run:
            remove_tree(ssd_dir)
        reclaimed += 1
    return reclaimed


def _evaluate_anomaly(config: Config, store: StateStore, ssd_referenced: set[str]) -> int:
    """Set or clear the anomaly marker from the live tick view.

    The unrecoverable case: a live symlink resolves into the SSD, but the
    infohash is neither hot in the DB nor backed by a sidecar — so its
    link→bulk mapping is lost and we cannot demote it cleanly. This is the
    silent footgun made visible. With the live qB data in hand each tick, the
    marker self-corrects: it is set while such a torrent exists and cleared
    once it no longer does.
    """
    hot = set(store.hot_infohashes())
    unrecoverable = [
        ih
        for ih in sorted(ssd_referenced)
        if ih not in hot and recovery.read_meta(config.ssd_cache_dir, ih) is None
    ]
    if config.dry_run:
        return len(unrecoverable)
    if unrecoverable:
        recovery.set_anomaly(
            config.ssd_cache_dir,
            "live symlinks point into the SSD with no recoverable mapping:\n"
            + "\n".join(unrecoverable),
        )
        log.error("tick.anomaly_present", count=len(unrecoverable))
    else:
        recovery.clear_anomaly(config.ssd_cache_dir)
    return len(unrecoverable)


def _free_ssd_bytes(config: Config, store: StateStore) -> int:
    """Bytes still spendable for new promotions."""
    used = store.hot_total_bytes()
    quota_b = int(config.quota_gb * 1024**3)
    min_free_b = int(config.min_free_gb * 1024**3)
    free = shutil.disk_usage(config.ssd_cache_dir).free
    return max(0, min(quota_b - used, free - min_free_b))


async def _tick(config: Config, store: StateStore) -> None:
    now = int(time.time())
    window_seconds = config.hotness.window_days * 86_400

    # 1. Pre-load bulk_targets for every already-hot torrent so the resolver
    #    can recover bulk paths even when readlink() now points into the SSD.
    hot_bulk_maps = await asyncio.to_thread(store.hot_bulk_maps)

    # 2. Poll instances in parallel.
    instance_results = await asyncio.gather(
        *(_collect_instance(i, config, hot_bulk_maps) for i in config.instances),
        return_exceptions=True,
    )

    all_resolved: list[ResolvedTorrent] = []
    ssd_referenced: set[str] = set()
    poll_ok = True
    for instance, result in zip(config.instances, instance_results, strict=True):
        if isinstance(result, BaseException):
            log.error("instance.poll_failed", instance=instance.name, error=str(result))
            poll_ok = False
            continue
        snapshots, resolved_list, refs = result
        ssd_referenced |= refs
        for infohash, uploaded, upspeed in snapshots:
            await asyncio.to_thread(
                store.record,
                instance=instance.name,
                infohash=infohash,
                ts=now,
                uploaded_session=uploaded,
                upspeed=upspeed,
            )
        all_resolved.extend(resolved_list)
        log.info(
            "tick.snapshot",
            instance=instance.name,
            snapshots=len(snapshots),
            resolved=len(resolved_list),
        )

    # 3. Aggregate by infohash.
    logical = aggregate(all_resolved)
    log.info("tick.aggregate", logical_torrents=len(logical), per_instance=len(all_resolved))

    # 4. Prune old snapshots.
    pruned = await asyncio.to_thread(store.prune, before_ts=now - window_seconds)
    if pruned:
        log.info("tick.pruned", rows=pruned)

    # 5. Build per-infohash candidates + bootstrap unknown tiers.
    candidates = _build_candidates(
        logical, store, now_ts=now, window_seconds=window_seconds
    )
    _bootstrap_tier(candidates, logical, store, now_ts=now)

    # 6. Cleanup orphans (hot tier but no live torrent) — only when every
    #    instance polled cleanly. With a down instance, `logical` omits its
    #    torrents, which would make us drop the SSD copy of content that is
    #    still being seeded there.
    if poll_ok:
        orphans_dropped = await asyncio.to_thread(
            _cleanup_orphans, set(logical.keys()), config, store
        )
        if orphans_dropped:
            log.info("tick.orphans_cleaned", count=orphans_dropped)

    # 7. Demote first.
    demotions = select_demotions(
        candidates,
        now_ts=now,
        demote_max_mb=config.hotness.demote_max_upload_mb,
        min_hot_minutes=config.hotness.min_hot_minutes,
    )
    for c in demotions:
        lt = logical[c.infohash]
        try:
            await asyncio.to_thread(demote, lt.layouts, dry_run=config.dry_run)
        except Exception:
            log.exception("demote.failed", infohash=c.infohash)
            continue
        if not config.dry_run:
            store.set_tier(infohash=c.infohash, tier="cold", since_ts=now, ssd_bytes=0)
    if demotions:
        log.info("tick.demoted", count=len(demotions))

    # 7b. Reclaim orphaned SSD dirs + (re)evaluate the anomaly marker, but
    #     only when every instance polled cleanly — a down instance would
    #     make `ssd_referenced` incomplete and risk reclaiming a dir its
    #     torrents still use.
    if poll_ok:
        reclaimed = await asyncio.to_thread(
            _cleanup_fs_orphans, config, store, ssd_referenced
        )
        if reclaimed:
            log.info("tick.fs_orphans_reclaimed", count=reclaimed)
        await asyncio.to_thread(_evaluate_anomaly, config, store, ssd_referenced)
    else:
        log.warning("tick.skip_orphan_reclaim_poll_incomplete")

    # 8. Recompute headroom.
    available = await asyncio.to_thread(_free_ssd_bytes, config, store)
    log.info("tick.headroom_bytes", bytes=available)

    # 9. Promote within headroom.
    max_size_bytes = (
        int(config.max_torrent_size_gb * 1024**3)
        if config.max_torrent_size_gb is not None
        else None
    )
    promotions = select_promotions(
        candidates,
        now_ts=now,
        promote_min_mb=config.hotness.promote_min_upload_mb,
        min_cold_minutes=config.hotness.min_cold_minutes,
        available_bytes=available,
        max_concurrent=config.max_concurrent_promotions,
        max_size_bytes=max_size_bytes,
    )
    for c in promotions:
        lt = logical[c.infohash]
        try:
            await asyncio.to_thread(promote, lt.layouts, now_ts=now, dry_run=config.dry_run)
        except Exception:
            log.exception("promote.failed", infohash=c.infohash)
            continue
        if not config.dry_run:
            store.set_tier(
                infohash=c.infohash,
                tier="hot",
                since_ts=now,
                ssd_bytes=c.size_bytes,
                bulk_targets=bulk_targets_of(lt.layouts),
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

    config.ssd_cache_dir.mkdir(parents=True, exist_ok=True)
    marker = config.ssd_cache_dir / ".ssd-mount-ok"
    if not marker.exists():
        marker.touch()

    store = StateStore(config.state_db)

    # Reconcile the DB against the filesystem before the first tick. This
    # rebuilds hot tier rows from the on-disk sidecars when the DB is fresh
    # or was replaced (the incident that motivated the sidecar), and undoes
    # promotions whose SSD copy disappeared. Anomalies it cannot repair are
    # surfaced via the healthcheck.
    await asyncio.to_thread(reconcile_startup, config, store)

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

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=config.poll_interval_sec)
    finally:
        store.close()
        log.info("daemon.stop")
