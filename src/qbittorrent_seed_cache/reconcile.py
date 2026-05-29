"""Startup reconciliation between the DB and the SSD filesystem.

Run once, before the first poll tick. It converges three drift scenarios
that a plain restart of the daemon would otherwise mishandle:

A. **DB fresh / lost, SSD intact.** The cache directories and their
   ``.qbsc-meta.json`` sidecars are on disk but the DB has no hot tier rows
   (new deployment, replaced/corrupt DB). We rebuild the hot tier rows from
   the sidecars, restoring the link→bulk accounting. Without this the daemon
   would think the SSD is empty and over-promote on top of the existing
   cache until the disk fills. This is the failure that motivated the whole
   sidecar mechanism.

B. **SSD dir deleted, DB intact.** A hot tier row exists but its
   ``<infohash>/`` directory is gone (manual cleanup, disk wipe). The
   torrent's symlinks now dangle into a missing SSD path. We retarget them
   back to the bulk filesystem using the DB's ``bulk_targets`` and drop the
   tier row — i.e. a demotion with no SSD to remove.

C. **Both lost (unrecoverable).** An SSD directory *with real content* but
   neither a usable sidecar nor a DB row, or a hot DB row with no
   ``bulk_targets`` and a missing SSD dir. The link→bulk mapping is simply
   gone; we cannot repair it automatically. We log the affected infohash and
   raise an anomaly marker so the container healthcheck reports unhealthy,
   leaving the operator to repair the symlinks by hand (or re-download).
   (Empty / payload-less directories are *not* anomalies — they carry no
   data and nothing references them, so they are skipped silently.)

As a forward-migration nicety, any currently-hot torrent that the DB knows
about but that predates the sidecar mechanism gets a sidecar written from
the DB row, so a *future* DB loss is covered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from . import recovery
from .config import Config
from .state import StateStore
from .symlinks import atomic_retarget, relative_target

log = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    rebuilt: int = 0          # hot tier rows rebuilt from sidecars (case A)
    sidecars_written: int = 0  # forward-migration sidecars written
    redemoted: int = 0        # hot rows whose SSD dir vanished, demoted (case B)
    anomalies: list[str] = field(default_factory=list)  # unrecoverable (case C)


def _rebuild_from_sidecar(
    store: StateStore, infohash: str, meta: recovery.SidecarMeta
) -> None:
    store.set_tier(
        infohash=infohash,
        tier="hot",
        since_ts=meta.since_ts,
        ssd_bytes=meta.ssd_bytes,
        bulk_targets=meta.bulk_targets,
    )


def _retarget_to_bulk(infohash: str, bulk_targets: dict[str, str], *, dry_run: bool) -> int:
    """Point each link back at its relative bulk target. Returns links fixed."""
    fixed = 0
    for link_str, bulk_str in bulk_targets.items():
        link = Path(link_str)
        if not link.is_symlink():
            # Real file or gone — nothing safe to do.
            continue
        rel = relative_target(link.parent, Path(bulk_str))
        log.info(
            "reconcile.retarget_to_bulk",
            infohash=infohash,
            link=link_str,
            bulk=bulk_str,
            dry_run=dry_run,
        )
        if not dry_run:
            atomic_retarget(link, rel)
        fixed += 1
    return fixed


def reconcile(config: Config, store: StateStore) -> ReconcileReport:
    """Pure-ish reconciliation pass. Returns a report; does not touch the marker."""
    report = ReconcileReport()
    ssd_cache_dir = config.ssd_cache_dir
    dry_run = config.dry_run

    seen_dirs: set[str] = set()

    # --- Pass A: SSD dirs → DB ------------------------------------------
    for infohash, ssd_dir in recovery.iter_ssd_infohash_dirs(ssd_cache_dir):
        seen_dirs.add(infohash)
        meta = recovery.read_meta(ssd_cache_dir, infohash)
        tier = store.get_tier(infohash=infohash)

        if meta is None:
            # SSD dir without a usable sidecar.
            if not recovery.ssd_dir_has_payload(ssd_dir):
                # Empty / payload-less directory (a stray mountpoint artifact,
                # a leftover from an earlier layout, or a dir whose files were
                # removed out of band). There is nothing to recover and
                # nothing at risk — skip it quietly rather than flagging a
                # false anomaly.
                log.debug("reconcile.skip_empty_dir", infohash=infohash, ssd_dir=str(ssd_dir))
                continue
            if tier is not None and tier.tier == "hot" and tier.bulk_targets:
                # Legacy promotion (pre-sidecar). DB still has the mapping —
                # forward-migrate by writing a sidecar from the DB row.
                log.info("reconcile.forward_migrate_sidecar", infohash=infohash)
                if not dry_run:
                    recovery.write_meta(
                        ssd_cache_dir,
                        infohash=infohash,
                        since_ts=tier.since_ts,
                        ssd_bytes=recovery.ssd_dir_bytes(ssd_dir),
                        bulk_targets=tier.bulk_targets,
                    )
                report.sidecars_written += 1
            else:
                log.error("reconcile.unattributable_ssd_dir", infohash=infohash, ssd_dir=str(ssd_dir))
                report.anomalies.append(f"ssd dir without sidecar or DB mapping: {infohash}")
            continue

        # Sidecar present and valid.
        if tier is None or tier.tier != "hot" or not tier.bulk_targets:
            log.info(
                "reconcile.rebuild_tier_from_sidecar",
                infohash=infohash,
                files=len(meta.bulk_targets),
                ssd_bytes=meta.ssd_bytes,
                dry_run=dry_run,
            )
            if not dry_run:
                _rebuild_from_sidecar(store, infohash, meta)
            report.rebuilt += 1

    # --- Pass B: DB hot rows whose SSD dir is gone ----------------------
    for infohash in store.hot_infohashes():
        if infohash in seen_dirs:
            continue
        ssd_dir = ssd_cache_dir / infohash
        if recovery.ssd_dir_has_payload(ssd_dir):
            # Present after all (e.g. dir exists but iterating skipped it for
            # some reason) — leave it for the normal tick to handle.
            continue
        tier = store.get_tier(infohash=infohash)
        if tier is None or not tier.bulk_targets:
            log.error("reconcile.missing_ssd_no_mapping", infohash=infohash)
            report.anomalies.append(f"hot DB row, SSD dir gone, no bulk_targets: {infohash}")
            continue
        log.warning("reconcile.demote_missing_ssd", infohash=infohash, ssd_dir=str(ssd_dir))
        _retarget_to_bulk(infohash, tier.bulk_targets, dry_run=dry_run)
        if not dry_run:
            store.delete_tier(infohash=infohash)
        report.redemoted += 1

    return report


def reconcile_startup(config: Config, store: StateStore) -> ReconcileReport:
    """Run reconciliation and (un)set the anomaly marker based on the result."""
    report = reconcile(config, store)

    log.info(
        "reconcile.done",
        rebuilt=report.rebuilt,
        sidecars_written=report.sidecars_written,
        redemoted=report.redemoted,
        anomalies=len(report.anomalies),
        dry_run=config.dry_run,
    )

    if config.dry_run:
        return report

    if report.anomalies:
        detail = "\n".join(report.anomalies)
        recovery.set_anomaly(config.ssd_cache_dir, detail)
        log.error("reconcile.anomalies_present", count=len(report.anomalies))
    else:
        recovery.clear_anomaly(config.ssd_cache_dir)

    return report
