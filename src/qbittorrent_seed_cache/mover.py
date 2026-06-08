"""Promotion/demotion of a *logical* torrent (one infohash, N instances).

The SSD cache stores one copy per infohash at `ssd_cache_dir/<infohash>/...`,
shared by all qB instances that hold that infohash. Each instance has its
own symlinks in its own `torrents/<release>/` dir; the mover retargets all
of them in lockstep.

Cold → Hot (`promote`):
  1. For each unique SSD destination path among the layouts, copy bulk→SSD
     once. The copy is atomic (tmp + rename) and skipped if the destination
     already exists with the right size (resumes after a crash).
  2. Write the recovery sidecar (`.qbsc-meta.json`) recording the
     link→bulk mapping, *before* the symlinks are retargeted.
  3. Retarget every layout's symlink to its (absolute) SSD destination.

The ordering of 2 before 3 is deliberate: the sidecar must be on disk
before any symlink points into the SSD, so that a crash at any point leaves
state from which the filesystem alone can be reconciled (see
:mod:`qbittorrent_seed_cache.recovery`).

Hot → Cold (`demote`):
  1. Retarget every layout's symlink to a relative path into the bulk
     filesystem.
  2. Remove the SSD `<infohash>/` directory once (this also removes the
     sidecar that lives inside it).

Both helpers expect every layout in the list to share the same infohash.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

from . import recovery
from .symlinks import atomic_retarget, relative_target, remove_tree, safe_copy

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TorrentLayout:
    """Per-file resolved paths within one qB instance."""

    instance: str
    infohash: str
    # Where the symlink lives (the path qB uses to seed from).
    link: Path
    # The real file in the bulk filesystem (NFS/HDD).
    bulk_target: Path
    # The SSD copy path. Same for every instance with this infohash+rel.
    ssd_target: Path


def _check_single_infohash(layouts: Iterable[TorrentLayout]) -> str:
    infohashes = {layout.infohash for layout in layouts}
    if len(infohashes) != 1:
        raise ValueError(f"layouts must share a single infohash, got {infohashes}")
    return next(iter(infohashes))


def _ssd_root(layout: TorrentLayout) -> Path:
    """Return the <ssd_cache_dir>/<infohash>/ directory containing ssd_target.

    Since ssd_target = ssd_cache_dir / infohash / <rel>, the wanted dir is
    the ancestor whose own basename equals the infohash.
    """
    for parent in layout.ssd_target.parents:
        if parent.name == layout.infohash:
            return parent
    raise ValueError(
        f"ssd_target {layout.ssd_target} does not contain infohash dir {layout.infohash!r}"
    )


def bulk_targets_of(layouts: Iterable[TorrentLayout]) -> dict[str, str]:
    """Build the ``{link path: bulk path}`` map persisted for a hot torrent.

    Shared by the DB tier row and the on-disk recovery sidecar so the two
    never drift.
    """
    return {str(layout.link): str(layout.bulk_target) for layout in layouts}


def retarget_to_bulk(
    infohash: str, bulk_targets: dict[str, str], *, dry_run: bool = False
) -> int:
    """Point each link in ``bulk_targets`` back at its relative bulk file.

    A "demote with nothing to copy": used when we must drop an SSD copy but
    only have the persisted ``{link: bulk}`` mapping (a DB tier row or an
    on-disk sidecar), not live qB layouts. Returns the number of links
    retargeted. Links that are no longer symlinks (replaced by a real file,
    or already gone) are skipped — there is nothing safe to do with them.
    """
    fixed = 0
    for link_str, bulk_str in bulk_targets.items():
        link = Path(link_str)
        if not link.is_symlink():
            continue
        rel = relative_target(link.parent, Path(bulk_str))
        log.info(
            "retarget_to_bulk",
            infohash=infohash,
            link=link_str,
            bulk=bulk_str,
            dry_run=dry_run,
        )
        if not dry_run:
            atomic_retarget(link, rel)
        fixed += 1
    return fixed


def promote(layouts: list[TorrentLayout], *, now_ts: int, dry_run: bool = False) -> int:
    """Promote a logical torrent to the SSD. Returns bytes copied (sum of unique destinations).

    Idempotent w.r.t. SSD content: if the SSD copy already exists with the
    right size, only the symlinks are retargeted.

    ``now_ts`` is stamped into the recovery sidecar as ``since_ts``.
    """
    if not layouts:
        return 0
    infohash = _check_single_infohash(layouts)

    # Group by SSD destination to dedup copies across instances sharing the
    # same infohash.
    by_dest: dict[Path, TorrentLayout] = {}
    for layout in layouts:
        by_dest.setdefault(layout.ssd_target, layout)

    total = 0
    for dest, layout in by_dest.items():
        size = layout.bulk_target.stat().st_size
        if dest.exists() and dest.stat().st_size == size:
            log.info(
                "promote.copy_skip",
                infohash=infohash,
                ssd=str(dest),
                bytes=size,
            )
        else:
            log.info(
                "promote.copy",
                infohash=infohash,
                bulk=str(layout.bulk_target),
                ssd=str(dest),
                bytes=size,
                dry_run=dry_run,
            )
            if not dry_run:
                safe_copy(layout.bulk_target, dest)
        total += size

    # Write the recovery sidecar BEFORE retargeting any symlink, so a crash
    # mid-retarget still leaves the filesystem self-describing. ssd_cache_dir
    # is the parent of the <infohash> dir.
    ssd_cache_dir = _ssd_root(layouts[0]).parent
    bulk_targets = bulk_targets_of(layouts)
    log.info(
        "promote.write_meta",
        infohash=infohash,
        files=len(bulk_targets),
        bytes=total,
        dry_run=dry_run,
    )
    if not dry_run:
        recovery.write_meta(
            ssd_cache_dir,
            infohash=infohash,
            since_ts=now_ts,
            ssd_bytes=total,
            bulk_targets=bulk_targets,
        )

    for layout in layouts:
        log.info(
            "promote.retarget",
            instance=layout.instance,
            infohash=infohash,
            link=str(layout.link),
            ssd=str(layout.ssd_target),
            dry_run=dry_run,
        )
        if not dry_run:
            atomic_retarget(layout.link, layout.ssd_target)

    log.info("promote.ok", infohash=infohash, instances=sorted({layout.instance for layout in layouts}), bytes=total)
    return total


def demote(layouts: list[TorrentLayout], *, dry_run: bool = False) -> int:
    """Demote a logical torrent: retarget all symlinks to bulk, drop SSD copy."""
    if not layouts:
        return 0
    infohash = _check_single_infohash(layouts)

    freed = 0
    # Sum the unique SSD bytes (one copy per (infohash, rel file)).
    seen: set[Path] = set()
    for layout in layouts:
        if layout.ssd_target in seen:
            continue
        seen.add(layout.ssd_target)
        if layout.ssd_target.exists():
            freed += layout.ssd_target.stat().st_size

    for layout in layouts:
        rel = relative_target(layout.link.parent, layout.bulk_target)
        log.info(
            "demote.retarget",
            instance=layout.instance,
            infohash=infohash,
            link=str(layout.link),
            bulk=str(layout.bulk_target),
            dry_run=dry_run,
        )
        if not dry_run:
            atomic_retarget(layout.link, rel)

    ssd_root = _ssd_root(layouts[0])
    log.info("demote.rm_ssd", infohash=infohash, ssd_root=str(ssd_root), bytes=freed, dry_run=dry_run)
    if not dry_run:
        remove_tree(ssd_root)

    log.info("demote.ok", infohash=infohash, instances=sorted({layout.instance for layout in layouts}), bytes=freed)
    return freed
