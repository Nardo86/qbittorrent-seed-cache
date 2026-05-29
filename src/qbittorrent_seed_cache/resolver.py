"""Resolve a qB TorrentInfo + file list into concrete TorrentLayouts.

For each file in a torrent we produce a TorrentLayout with:

  link        — the symlink path on the host (== container_save_path/name,
                mapped via path_map). This is what qB resolves to read.
  bulk_target — what the link currently points to in the bulk filesystem.
                Resolved by readlink() on the link, then normalised.
  ssd_target  — the planned SSD path under <ssd_cache_dir>/<infohash>/<name>.

The link is the source of truth for "where the bulk file lives", because
after the post-import step the entry under `torrents/<release>/` is *always*
a symlink to the canonical media-library file. We deliberately do not look
at sonarr/radarr DBs — `readlink` is the simple, robust source of truth.

If the link is not a symlink (e.g. the torrent has never been imported by
sonarr/radarr, or post-import hasn't run yet), we skip that torrent — the
mover only manages entries that already follow the symlink convention.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from .mover import TorrentLayout
from .paths import map_to_host

if TYPE_CHECKING:
    from .qbit_client import TorrentInfo

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedTorrent:
    """All the per-file layouts for one torrent in one qB instance."""

    instance: str
    infohash: str
    layouts: list[TorrentLayout]
    total_bytes: int

    @property
    def is_hot_on_ssd(self) -> bool:
        """True if every link currently resolves into the SSD cache dir."""
        return all(
            _link_target_is(layout.link, layout.ssd_target.parent.parent)
            for layout in self.layouts
        )


@dataclass(frozen=True, slots=True)
class LogicalTorrent:
    """The same infohash possibly seeded by multiple qB instances.

    SSD storage is deduplicated by infohash: one copy under
    `ssd_cache_dir/<infohash>/...`, shared by symlinks from every instance.
    """

    infohash: str
    size_bytes: int
    # All per-file layouts across every instance hosting this infohash.
    # The `instance` field on each TorrentLayout identifies which qB it
    # belongs to.
    layouts: list[TorrentLayout]
    # Instance names hosting this infohash, in stable order.
    instances: tuple[str, ...]

    @property
    def is_hot_on_ssd(self) -> bool:
        """True if every link across every instance resolves into the SSD cache."""
        return all(
            _link_target_is(layout.link, layout.ssd_target.parent.parent)
            for layout in self.layouts
        )


def aggregate(per_instance: list[ResolvedTorrent]) -> dict[str, LogicalTorrent]:
    """Group per-instance ResolvedTorrents by infohash into LogicalTorrents.

    Size is taken from the first instance (same infohash → same size).
    Layouts from every instance are concatenated.
    """
    grouped: dict[str, list[ResolvedTorrent]] = {}
    for r in per_instance:
        grouped.setdefault(r.infohash, []).append(r)

    out: dict[str, LogicalTorrent] = {}
    for infohash, parts in grouped.items():
        layouts = [layout for r in parts for layout in r.layouts]
        instances = tuple(sorted({r.instance for r in parts}))
        size = parts[0].total_bytes
        out[infohash] = LogicalTorrent(
            infohash=infohash, size_bytes=size, layouts=layouts, instances=instances
        )
    return out


def _link_target_is(link: Path, expected_root: Path) -> bool:
    if not link.is_symlink():
        return False
    real = Path(os.path.realpath(link))
    try:
        real.relative_to(expected_root)
        return True
    except ValueError:
        return False


def resolve(
    *,
    instance: str,
    torrent: TorrentInfo,
    files: list[dict[str, Any]],
    ssd_cache_dir: Path,
    path_map: dict[str, str],
    managed_paths: list[Path],
    hot_bulk_map: dict[str, str] | None = None,
) -> ResolvedTorrent | None:
    """Resolve a torrent to its per-file layouts, or None to skip.

    Per-file skip: if a single file in the torrent is not a symlink (e.g. a
    `.nfo` left as a real file by the migration), or is missing, or resolves
    outside `managed_paths`, that file is omitted. The torrent as a whole is
    only skipped when *no* file remains.

    `hot_bulk_map`: for a torrent that's already promoted, the link points
    *into* `ssd_cache_dir`, so `readlink` can't tell us where the bulk file
    lives. The daemon precomputes a `{link_host_path: bulk_host_path}` map
    from the persisted tier rows and passes it here; when we see a link
    that resolves into the SSD, we look the bulk up in this map. If the
    link is missing from the map, we treat the file as corrupted state
    (skip + log) — better than misclassifying the torrent as orphan.

    Rationale: real torrents often bundle small extras (.nfo, .txt, covers)
    next to the main media file. Forcing the entire torrent to be all-or-
    nothing would exclude most multi-file torrents from the cache. Promoting
    just the symlinked files is fine — qB seeds extras directly from bulk.
    """
    save_path_host = map_to_host(torrent.save_path, path_map)

    layouts: list[TorrentLayout] = []
    total = 0
    skipped = 0
    for f in files:
        rel = f["name"]  # POSIX relative path within save_path
        size = int(f["size"])
        link = save_path_host / rel

        if not link.exists() and not link.is_symlink():
            log.debug(
                "resolve.skip_file_missing",
                instance=instance,
                infohash=torrent.hash,
                link=str(link),
            )
            skipped += 1
            continue

        if not link.is_symlink():
            log.debug(
                "resolve.skip_file_not_symlink",
                instance=instance,
                infohash=torrent.hash,
                link=str(link),
            )
            skipped += 1
            continue

        link_target = Path(os.path.realpath(link))

        # If the symlink resolves into the SSD cache, this is an already-hot
        # torrent. Look up the canonical bulk_target from the precomputed map.
        if _is_under(link_target, ssd_cache_dir):
            bulk_target_str = (hot_bulk_map or {}).get(str(link))
            if bulk_target_str is None:
                # The symlink points into the SSD but neither the DB nor a
                # reconciled sidecar told us its bulk origin. Startup
                # reconciliation normally rebuilds this mapping, so reaching
                # here at steady state means the torrent's accounting is lost
                # — log loudly (this is the silent footgun that once filled
                # the disk) rather than skipping quietly.
                log.error(
                    "resolve.skip_file_hot_unknown_bulk",
                    instance=instance,
                    infohash=torrent.hash,
                    link=str(link),
                    ssd_target=str(link_target),
                )
                skipped += 1
                continue
            bulk_target = Path(bulk_target_str)
        else:
            bulk_target = link_target

        if managed_paths and not any(_is_under(bulk_target, mp) for mp in managed_paths):
            log.debug(
                "resolve.skip_file_unmanaged",
                instance=instance,
                infohash=torrent.hash,
                bulk=str(bulk_target),
            )
            skipped += 1
            continue

        ssd_target = ssd_cache_dir / torrent.hash / rel
        layouts.append(
            TorrentLayout(
                instance=instance,
                infohash=torrent.hash,
                link=link,
                bulk_target=bulk_target,
                ssd_target=ssd_target,
            )
        )
        total += size

    if not layouts:
        log.debug(
            "resolve.skip_torrent_no_eligible_files",
            instance=instance,
            infohash=torrent.hash,
            skipped_files=skipped,
        )
        return None

    if skipped:
        log.debug(
            "resolve.partial",
            instance=instance,
            infohash=torrent.hash,
            included=len(layouts),
            skipped=skipped,
        )

    return ResolvedTorrent(
        instance=instance, infohash=torrent.hash, layouts=layouts, total_bytes=total
    )


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False
