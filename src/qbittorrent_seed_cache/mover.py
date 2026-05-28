"""Promotion/demotion primitives.

A torrent's content_path (from qB) is a path *inside* the qB container.
Both qB and the mover see the same absolute path for `ssd_cache_dir` and
the same absolute path for the bulk filesystem (bind-mount discipline).
The symlink in `torrents/<release>/<file>` itself is what qB resolves at
seed time, so retargeting it is sufficient — qB does not cache the inode.

Cold → Hot:
  1. Compute SSD destination: ssd_cache_dir / <infohash> / <relative content>
  2. Copy bulk → ssd (atomic via tmp + rename).
  3. Retarget link → ssd (atomic).

Hot → Cold:
  1. Retarget link → bulk (atomic, relative target preferred when on the
     same fs as the link).
  2. rm -rf ssd_cache_dir / <infohash>.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from .symlinks import atomic_retarget, relative_target, remove_tree, safe_copy

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TorrentLayout:
    """Resolved paths for a single torrent."""

    instance: str
    infohash: str
    # Where the symlink lives (the path qB uses to seed from).
    link: Path
    # The real file in the bulk filesystem (NFS/HDD).
    bulk_target: Path
    # The SSD copy path. Exists only when the torrent is hot.
    ssd_target: Path


def promote(layout: TorrentLayout, *, dry_run: bool = False) -> int:
    """Copy bulk → SSD and retarget the symlink. Returns bytes copied."""
    size = layout.bulk_target.stat().st_size
    log.info(
        "promote.start",
        instance=layout.instance,
        infohash=layout.infohash,
        bytes=size,
        link=str(layout.link),
        bulk=str(layout.bulk_target),
        ssd=str(layout.ssd_target),
        dry_run=dry_run,
    )
    if dry_run:
        return size

    safe_copy(layout.bulk_target, layout.ssd_target)
    atomic_retarget(layout.link, layout.ssd_target)  # absolute target (cross-fs)
    log.info("promote.ok", instance=layout.instance, infohash=layout.infohash, bytes=size)
    return size


def demote(layout: TorrentLayout, *, dry_run: bool = False) -> int:
    """Retarget the symlink back to bulk and drop the SSD copy. Returns bytes freed."""
    freed = 0
    if layout.ssd_target.exists():
        freed = layout.ssd_target.stat().st_size

    log.info(
        "demote.start",
        instance=layout.instance,
        infohash=layout.infohash,
        bytes=freed,
        link=str(layout.link),
        bulk=str(layout.bulk_target),
        dry_run=dry_run,
    )
    if dry_run:
        return freed

    rel = relative_target(layout.link.parent, layout.bulk_target)
    atomic_retarget(layout.link, rel)
    remove_tree(layout.ssd_target.parent)  # remove <infohash>/ dir
    log.info("demote.ok", instance=layout.instance, infohash=layout.infohash, bytes=freed)
    return freed
