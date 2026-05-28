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
from typing import Any

import structlog

from .mover import TorrentLayout
from .paths import map_to_host
from .qbit_client import TorrentInfo

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedTorrent:
    """All the per-file layouts for one torrent, plus aggregate size."""

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
) -> ResolvedTorrent | None:
    """Resolve a torrent to its per-file layouts, or None to skip."""
    save_path_host = map_to_host(torrent.save_path, path_map)

    layouts: list[TorrentLayout] = []
    total = 0
    for f in files:
        rel = f["name"]  # POSIX relative path within save_path
        size = int(f["size"])
        link = save_path_host / rel

        if not link.exists() and not link.is_symlink():
            log.debug(
                "resolve.skip_missing_link",
                instance=instance,
                infohash=torrent.hash,
                link=str(link),
            )
            return None

        if not link.is_symlink():
            log.debug(
                "resolve.skip_not_symlink",
                instance=instance,
                infohash=torrent.hash,
                link=str(link),
            )
            return None

        bulk_target = Path(os.path.realpath(link))

        if managed_paths and not any(_is_under(bulk_target, mp) for mp in managed_paths):
            log.debug(
                "resolve.skip_unmanaged",
                instance=instance,
                infohash=torrent.hash,
                bulk=str(bulk_target),
            )
            return None

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
        return None

    return ResolvedTorrent(
        instance=instance, infohash=torrent.hash, layouts=layouts, total_bytes=total
    )


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False
