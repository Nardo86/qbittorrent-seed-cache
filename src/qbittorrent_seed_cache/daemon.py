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
     score. (Old snapshots are pruned every tick; hourly, rows older than a
     day are thinned and the DB file vacuumed when mostly free pages.) The `instances` list on each candidate is informational.
  5. Bootstrap tier rows for previously-unknown infohashes from the
     current symlink state (is_hot_on_ssd).
  6. Cleanup orphans: infohashes with tier='hot' that no longer exist in
     any instance (probably removed from qB) — retarget their symlinks back
     to bulk (from the DB row / sidecar) first, then rm the SSD dir and drop
     the tier row. Retargeting before the rm means a *wrong* reclaim (e.g. a
     torrent only briefly absent during qB fastresume loading) degrades to a
     cache miss instead of dangling links + a lost mapping (issue #5).
  7. Apply demotions first (frees SSD bytes). Demotions and displacements
     are skipped when an instance poll failed: the scores would miss that
     instance's upload and its symlinks would be missing from the layouts.
  6b. Find live symlinks into the SSD whose link->bulk mapping is lost
     (anomaly). Their torrents are quarantined for the rest of the tick
     (never promoted, demoted or displaced) and the anomaly marker + a
     rate-limited error line report them (see `anomaly`).
  7b. Reclaim SSD dirs that are neither hot nor referenced by a live symlink.
     Steps 6, 6b (marker update) and 7b are skipped if any instance poll
     failed (the live set would be partial).
  8. Recompute available headroom.
  9. Apply promotions within headroom (greedy by hotness, capped by
     max_concurrent_promotions). On success, persist the bulk_targets
     map alongside the tier row.

Liveness: a heartbeat file is refreshed at every tick; the healthcheck
treats a stale heartbeat (not a data anomaly) as unhealthy.

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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from . import recovery
from .anomaly import AnomalyReporter, find_unmapped_links
from .config import Config, InstanceConfig
from .hotness import HotnessScore
from .hotness import score as score_history
from .mover import bulk_targets_of, demote, promote, retarget_to_bulk
from .qbit_client import QbitClient
from .reconcile import reconcile_startup
from .resolver import LogicalTorrent, ResolvedTorrent, aggregate, resolve, ssd_links
from .selector import (
    TorrentCandidate,
    select_demotions,
    select_displacements,
    select_promotions,
)
from .state import StateStore
from .symlinks import CopyInterrupted, remove_tree

log = structlog.get_logger(__name__)

# How long a partially-copied promotion (`.qbsc-promoting.json` present, no
# sidecar yet) is kept for resumption while its torrent is still in qB.
PROMOTION_RESUME_TTL_SEC = 24 * 3600
_PROGRESS_BEAT_SEC = 30
_PROGRESS_LOG_SEC = 60
# DB thinning + VACUUM check cadence.
DB_MAINTENANCE_INTERVAL_SEC = 3600


@dataclass
class DaemonRuntime:
    """State that outlives a single tick (one per daemon process)."""

    anomaly: AnomalyReporter = field(
        default_factory=lambda: AnomalyReporter(log_interval_sec=3600)
    )
    # Set on SIGTERM/SIGINT. Long copies run in a worker thread that the
    # event loop cannot cancel; they poll this between chunks so shutdown
    # completes within Docker's stop timeout and keeps the partial file.
    halt: threading.Event = field(default_factory=threading.Event)
    last_db_maintenance_ts: int = 0

    @classmethod
    def for_config(cls, config: Config) -> DaemonRuntime:
        return cls(anomaly=AnomalyReporter(log_interval_sec=config.anomaly_log_interval_sec))


def _heartbeat(config: Config, now_ts: int) -> None:
    """Refresh the liveness heartbeat read by the healthcheck.

    The deadline leaves room for a few slow ticks (polling hundreds of
    torrents, pruning a large window) on top of the poll interval.
    """
    grace = 3 * config.poll_interval_sec + 600
    recovery.write_heartbeat(
        config.ssd_cache_dir, now_ts=now_ts, stale_after_ts=now_ts + grace
    )


def _maintain_db(config: Config, store: StateStore, now_ts: int) -> None:
    """Thin old snapshots and give freed pages back to the filesystem."""
    compacted = 0
    if config.snapshot_full_resolution_hours > 0:
        compacted = store.compact_snapshots(
            before_ts=now_ts - config.snapshot_full_resolution_hours * 3600,
            bucket_seconds=config.snapshot_bucket_minutes * 60,
        )
    size_before, free_before = store.file_stats()
    vacuumed = store.vacuum_if_fragmented()
    size_after, _ = store.file_stats()
    if compacted or vacuumed:
        log.info(
            "db.maintenance",
            compacted_rows=compacted,
            vacuumed=vacuumed,
            size_before=size_before,
            free_before=free_before,
            size_after=size_after,
        )


def _copy_progress(config: Config, infohash: str) -> Callable[[int, int], None]:
    """Progress callback for a promotion copy: keeps the heartbeat fresh and
    logs progress at a bounded rate (a 20 GB copy can take many minutes)."""
    last_beat = last_log = time.monotonic()

    def on_progress(done: int, total: int) -> None:
        nonlocal last_beat, last_log
        t = time.monotonic()
        if t - last_beat >= _PROGRESS_BEAT_SEC:
            _heartbeat(config, int(time.time()))
            last_beat = t
        if t - last_log >= _PROGRESS_LOG_SEC:
            log.info("promote.copy_progress", infohash=infohash, done=done, bytes=total)
            last_log = t

    return on_progress


async def _collect_instance(
    instance: InstanceConfig,
    config: Config,
    hot_bulk_maps: dict[str, dict[str, str]],
) -> tuple[list[tuple[str, int, int]], list[ResolvedTorrent], dict[str, set[str]]]:
    """Poll one qB instance.

    Returns:
      - snapshots: list of (infohash, uploaded_session, upspeed) tuples to record.
      - resolved: list of ResolvedTorrent for torrents that follow the symlink
        convention.
      - ssd_links: ``{infohash: {link, ...}}`` for live symlinks that currently
        resolve into the SSD cache (whether or not their bulk origin could be
        recovered). Used to protect referenced cache dirs from orphan
        reclamation and to detect links whose mapping is lost.

    `hot_bulk_maps[infohash]` is the persisted {link_path: bulk_path} map for
    torrents already promoted to SSD; passed through to resolve() so that the
    bulk file can be recovered even when readlink() now points into the SSD.
    """
    snapshots: list[tuple[str, int, int]] = []
    resolved_list: list[ResolvedTorrent] = []
    into_ssd: dict[str, set[str]] = {}
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
            links = ssd_links(
                torrent=t,
                files=files,
                ssd_cache_dir=config.ssd_cache_dir,
                path_map=instance.path_map,
            )
            if links:
                into_ssd.setdefault(t.hash, set()).update(str(link) for link in links)
    return snapshots, resolved_list, into_ssd


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


def _bulk_targets_for(config: Config, store: StateStore, infohash: str) -> dict[str, str]:
    """Best-effort ``{link: bulk}`` map for a torrent about to lose its SSD copy.

    Prefers the DB tier row and falls back to the on-disk sidecar so we can
    retarget the symlinks back to bulk before reclaiming the dir, even when
    one of the two persisted copies is already missing. Returns ``{}`` when
    neither source has a usable mapping.
    """
    tier = store.get_tier(infohash=infohash)
    if tier is not None and tier.bulk_targets:
        return tier.bulk_targets
    meta = recovery.read_meta(config.ssd_cache_dir, infohash)
    if meta is not None:
        return meta.bulk_targets
    return {}


def _demote_logical(config: Config, store: StateStore, lt: LogicalTorrent) -> int:
    """Demote ``lt``, also retargeting persisted links its live layouts miss."""
    return demote(
        lt.layouts,
        dry_run=config.dry_run,
        extra_bulk_targets=_bulk_targets_for(config, store, lt.infohash),
    )


def _cleanup_orphans(
    live_infohashes: set[str], config: Config, store: StateStore
) -> int:
    """Drop SSD content + tier row for hot infohashes no longer in any qB instance.

    Before removing the SSD copy we retarget the torrent's symlinks back to
    bulk (from the DB row, falling back to the sidecar). If the torrent was
    only *temporarily* absent — e.g. qB answered the poll while still loading
    fastresume and returned a partial list — this reclaim is wrong, but with
    the links already pointing at the canonical bulk files the worst case
    degrades to a cache miss instead of dangling links plus a lost mapping
    (issue #5).
    """
    freed_count = 0
    for ih in store.hot_infohashes():
        if ih in live_infohashes:
            continue
        ssd_dir = config.ssd_cache_dir / ih
        bulk_targets = _bulk_targets_for(config, store, ih)
        log.info(
            "orphan.cleanup",
            infohash=ih,
            ssd_dir=str(ssd_dir),
            retargetable_links=len(bulk_targets),
            dry_run=config.dry_run,
        )
        if not config.dry_run:
            if bulk_targets:
                retarget_to_bulk(ih, bulk_targets, dry_run=False)
            remove_tree(ssd_dir)
            store.delete_tier(infohash=ih)
        freed_count += 1
    return freed_count


def _cleanup_fs_orphans(
    config: Config,
    store: StateStore,
    ssd_referenced: set[str],
    live_infohashes: set[str] | None = None,
    now_ts: int | None = None,
) -> int:
    """Reclaim `<ssd_cache_dir>/<infohash>/` dirs that nothing uses.

    A dir is in use iff its infohash is hot in the DB OR a live qB symlink
    currently resolves into it (`ssd_referenced`). Anything else is an orphan
    — typically a demote that crashed after retargeting the symlink to bulk
    but before removing the SSD copy, or a torrent removed from qB. Because we
    require it to be *unreferenced by any live symlink*, deleting it cannot
    dangle a seed. Reclaiming here (before the headroom recompute) returns the
    space to the promotion budget.

    Exception: a dir holding an *interrupted promotion* (intent marker, no
    sidecar yet) is kept while its torrent is still live in qB and the
    first attempt is younger than ``PROMOTION_RESUME_TTL_SEC``, so the next
    promotion resumes the partial copy instead of starting over. (Before
    this, a restart mid-copy meant: reclaim the fragment, copy from zero,
    get killed again — forever, for a 20 GB file.)
    """
    in_use = set(store.hot_infohashes()) | ssd_referenced
    live = live_infohashes or set()
    now = now_ts if now_ts is not None else int(time.time())
    reclaimed = 0
    for infohash, ssd_dir in recovery.iter_ssd_infohash_dirs(config.ssd_cache_dir):
        if infohash in in_use:
            continue
        started = recovery.read_promotion_intent_ts(config.ssd_cache_dir, infohash)
        if (
            started is not None
            and infohash in live
            and now - started < PROMOTION_RESUME_TTL_SEC
            and recovery.read_meta(config.ssd_cache_dir, infohash) is None
        ):
            log.info(
                "orphan.keep_pending_promotion",
                infohash=infohash,
                ssd_dir=str(ssd_dir),
                age_sec=now - started,
            )
            continue
        # If a sidecar survived, retarget any of its links back to bulk before
        # the rm. The dir is unreferenced by any *live* symlink, so this is a
        # no-op for healthy state; it only matters for a link left dangling
        # into this dir (e.g. a sibling reclaim that lost the DB row first),
        # which it heals instead of leaving broken. See issue #5.
        meta = recovery.read_meta(config.ssd_cache_dir, infohash)
        bulk_targets = meta.bulk_targets if meta is not None else {}
        log.info(
            "orphan.fs_reclaim",
            infohash=infohash,
            ssd_dir=str(ssd_dir),
            payload=recovery.ssd_dir_has_payload(ssd_dir),
            retargetable_links=len(bulk_targets),
            dry_run=config.dry_run,
        )
        if not config.dry_run:
            if bulk_targets:
                retarget_to_bulk(infohash, bulk_targets, dry_run=False)
            remove_tree(ssd_dir)
        reclaimed += 1
    return reclaimed


def _untracked_ssd_bytes(config: Config, store: StateStore, infohashes: set[str]) -> int:
    """SSD bytes held by quarantined torrents that the DB does not account for.

    A torrent whose mapping is lost is not hot in the DB, so its SSD dir (if
    it still exists) is invisible to ``hot_total_bytes``. Counting it here
    keeps the quota honest — the original disk-full incident was exactly
    promotions stacked on top of SSD content the DB had forgotten about.
    """
    hot = set(store.hot_infohashes())
    return sum(
        recovery.ssd_dir_bytes(config.ssd_cache_dir / ih) for ih in infohashes if ih not in hot
    )


def _free_ssd_bytes(config: Config, store: StateStore, *, untracked_bytes: int = 0) -> int:
    """Bytes still spendable for new promotions."""
    used = store.hot_total_bytes() + untracked_bytes
    quota_b = int(config.quota_gb * 1024**3)
    min_free_b = int(config.min_free_gb * 1024**3)
    free = shutil.disk_usage(config.ssd_cache_dir).free
    return max(0, min(quota_b - used, free - min_free_b))


async def _tick(
    config: Config, store: StateStore, runtime: DaemonRuntime | None = None
) -> None:
    rt = runtime if runtime is not None else DaemonRuntime.for_config(config)
    now = int(time.time())
    window_seconds = config.hotness.window_days * 86_400
    await asyncio.to_thread(_heartbeat, config, now)

    # 1. Pre-load bulk_targets for every already-hot torrent so the resolver
    #    can recover bulk paths even when readlink() now points into the SSD.
    hot_bulk_maps = await asyncio.to_thread(store.hot_bulk_maps)

    # 2. Poll instances in parallel.
    instance_results = await asyncio.gather(
        *(_collect_instance(i, config, hot_bulk_maps) for i in config.instances),
        return_exceptions=True,
    )

    all_resolved: list[ResolvedTorrent] = []
    into_ssd: dict[str, set[str]] = {}
    poll_ok = True
    for instance, result in zip(config.instances, instance_results, strict=True):
        if isinstance(result, BaseException):
            log.error("instance.poll_failed", instance=instance.name, error=str(result))
            poll_ok = False
            continue
        snapshots, resolved_list, refs = result
        for ih, links in refs.items():
            into_ssd.setdefault(ih, set()).update(links)
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
    if now - rt.last_db_maintenance_ts >= DB_MAINTENANCE_INTERVAL_SEC:
        rt.last_db_maintenance_ts = now
        try:
            await asyncio.to_thread(_maintain_db, config, store, now)
        except Exception:
            log.exception("db.maintenance_failed")

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

    # 6b. Links into the SSD with a lost mapping: quarantine their torrents
    #     for this tick and (only with a complete live view) update the marker.
    unmapped = await asyncio.to_thread(
        find_unmapped_links, config.ssd_cache_dir, store, into_ssd
    )
    quarantined = set(unmapped)
    if poll_ok:
        rt.anomaly.report(config.ssd_cache_dir, unmapped, now_ts=now, dry_run=config.dry_run)
    active = [c for c in candidates if c.infohash not in quarantined]
    untracked = (
        await asyncio.to_thread(_untracked_ssd_bytes, config, store, quarantined)
        if quarantined
        else 0
    )

    # 7. Demote first — only with a complete poll. With an instance down,
    #    its torrents' scores lose that instance's upload (spurious
    #    demotions) and its symlinks are missing from the live layouts.
    demotions = (
        select_demotions(
            active,
            now_ts=now,
            demote_max_mb=config.hotness.demote_max_upload_mb,
            min_hot_minutes=config.hotness.min_hot_minutes,
        )
        if poll_ok
        else []
    )
    for c in demotions:
        lt = logical[c.infohash]
        try:
            await asyncio.to_thread(_demote_logical, config, store, lt)
        except Exception:
            log.exception("demote.failed", infohash=c.infohash)
            continue
        if not config.dry_run:
            store.set_tier(infohash=c.infohash, tier="cold", since_ts=now, ssd_bytes=0)
    if demotions:
        log.info("tick.demoted", count=len(demotions))

    # 7b. Reclaim orphaned SSD dirs, but only when every instance polled
    #     cleanly — a down instance would make the referenced set incomplete
    #     and risk reclaiming a dir its torrents still use.
    if poll_ok:
        reclaimed = await asyncio.to_thread(
            _cleanup_fs_orphans, config, store, set(into_ssd), set(logical), now
        )
        if reclaimed:
            log.info("tick.fs_orphans_reclaimed", count=reclaimed)
    else:
        log.warning("tick.skip_demote_and_reclaim_poll_incomplete")

    # 8. Recompute headroom.
    available = await asyncio.to_thread(
        _free_ssd_bytes, config, store, untracked_bytes=untracked
    )
    log.info("tick.headroom_bytes", bytes=available, untracked_bytes=untracked)

    if quarantined and config.suspend_promotions_on_anomaly:
        # Fail-safe opt-in: while any mapping is lost, don't add anything new
        # to the SSD. Demotions above still ran (they only free space).
        log.warning("tick.promotions_suspended_anomaly", torrents=len(quarantined))
        await asyncio.to_thread(_heartbeat, config, int(time.time()))
        return

    max_size_bytes = (
        int(config.max_torrent_size_gb * 1024**3)
        if config.max_torrent_size_gb is not None
        else None
    )

    # 8c. Displacement: when the cache is full of lukewarm torrents that never
    #     drop below `demote_max`, evict the least *dense* hot ones to make room
    #     for denser cold candidates (density = upload/day per byte; a candidate
    #     must beat its victim's density by displacement_factor). Eviction frees
    #     SSD without an HDD read; the promote step below fills the headroom.
    displacements: list[TorrentCandidate] = []
    if poll_ok:
        displacements = select_displacements(
            active,
            now_ts=now,
            available_bytes=available,
            promote_min_mb=config.hotness.promote_min_upload_mb,
            min_hot_minutes=config.hotness.min_hot_minutes,
            min_cold_minutes=config.hotness.min_cold_minutes,
            displacement_factor=config.hotness.displacement_factor,
            max_promotions=config.max_concurrent_promotions,
            max_evictions=config.max_displacements_per_tick,
            max_size_bytes=max_size_bytes,
        )
    for c in displacements:
        lt = logical[c.infohash]
        try:
            await asyncio.to_thread(_demote_logical, config, store, lt)
        except Exception:
            log.exception("displace.failed", infohash=c.infohash)
            continue
        if not config.dry_run:
            store.set_tier(infohash=c.infohash, tier="cold", since_ts=now, ssd_bytes=0)
    if displacements:
        log.info("tick.displaced", count=len(displacements))
        available = await asyncio.to_thread(
            _free_ssd_bytes, config, store, untracked_bytes=untracked
        )
        log.info("tick.headroom_bytes", bytes=available, after="displace")

    # 9. Promote within headroom.
    promotions = select_promotions(
        active,
        now_ts=now,
        promote_min_mb=config.hotness.promote_min_upload_mb,
        min_cold_minutes=config.hotness.min_cold_minutes,
        available_bytes=available,
        max_concurrent=config.max_concurrent_promotions,
        max_size_bytes=max_size_bytes,
    )
    for c in promotions:
        if rt.halt.is_set():
            break
        lt = logical[c.infohash]
        try:
            await asyncio.to_thread(
                promote,
                lt.layouts,
                now_ts=now,
                dry_run=config.dry_run,
                stop=rt.halt,
                on_progress=_copy_progress(config, c.infohash),
            )
        except CopyInterrupted as exc:
            # Shutdown requested: the partial copy and the intent marker stay
            # on disk; the next start resumes this promotion.
            log.info("promote.interrupted", infohash=c.infohash, detail=str(exc))
            break
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
    await asyncio.to_thread(_heartbeat, config, int(time.time()))


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
    rt = DaemonRuntime.for_config(config)

    # Reconcile the DB against the filesystem before the first tick. This
    # rebuilds hot tier rows from the on-disk sidecars when the DB is fresh
    # or was replaced (the incident that motivated the sidecar), and undoes
    # promotions whose SSD copy disappeared. Anomalies it cannot repair are
    # surfaced via the healthcheck.
    await asyncio.to_thread(reconcile_startup, config, store)
    _heartbeat(config, int(time.time()))

    stop = asyncio.Event()

    def _request_stop() -> None:
        stop.set()
        rt.halt.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)

    try:
        while not stop.is_set():
            try:
                await _tick(config, store, rt)
            except Exception:
                log.exception("tick.failed")

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=config.poll_interval_sec)
    finally:
        store.close()
        log.info("daemon.stop")
