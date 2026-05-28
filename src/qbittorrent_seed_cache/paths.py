"""Path translation between qB container view and host view.

qBittorrent reports absolute paths as they exist *inside the qB container*
(e.g. /data/torrents/Film/X). The mover runs in a different container (or
on the host) and needs to translate those into paths it can read/write.

We use a `path_map` (dict[container_prefix, host_prefix]) with longest-prefix
matching. A torrent file at `/data/torrents/Film/X.mkv` under the map
`{"/data": "/mnt/media/storage"}` resolves to
`/mnt/media/storage/torrents/Film/X.mkv`.

If no prefix matches, we return the path unchanged — useful when the mover
shares the qB bind-mount layout 1:1 (no translation needed).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def map_to_host(container_path: str | Path, path_map: dict[str, str]) -> Path:
    """Translate a container-side absolute POSIX path to its host equivalent."""
    p = PurePosixPath(str(container_path))
    if not p.is_absolute():
        raise ValueError(f"map_to_host expects absolute paths, got {container_path!r}")

    best_prefix: PurePosixPath | None = None
    best_target: str | None = None
    for prefix_str, target_str in path_map.items():
        prefix = PurePosixPath(prefix_str)
        try:
            p.relative_to(prefix)
        except ValueError:
            continue
        if best_prefix is None or len(prefix.parts) > len(best_prefix.parts):
            best_prefix = prefix
            best_target = target_str

    if best_prefix is None or best_target is None:
        return Path(str(p))

    rel = p.relative_to(best_prefix)
    return Path(best_target) / rel
