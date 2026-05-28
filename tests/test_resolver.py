"""resolve(): per-file skip semantics."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from qbittorrent_seed_cache.resolver import resolve


@dataclass
class _FakeTorrent:
    """Duck-typed stand-in for qbit_client.TorrentInfo (avoids httpx import in tests)."""

    hash: str
    save_path: str


def _setup_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a fake bulk fs + qB-style torrents/ dir with mixed entries."""
    bulk_root = tmp_path / "mnt" / "media" / "storage"
    films = bulk_root / "Films" / "X"
    films.mkdir(parents=True)
    (films / "movie.mkv").write_bytes(b"video-bytes" * 1000)

    save_dir = bulk_root / "torrents" / "rel-X"
    save_dir.mkdir(parents=True)

    # Symlinked media file (the kind we care about).
    os.symlink(os.path.relpath(films / "movie.mkv", save_dir), save_dir / "movie.mkv")

    # Real file left in place (the kind migration didn't touch).
    (save_dir / "movie.nfo").write_text("metadata\n")

    return bulk_root, save_dir


def _qb_save_path(save_dir: Path, bulk_root: Path) -> str:
    """The path qB sees: /data/... where /data maps to bulk_root."""
    return "/data/" + str(save_dir.relative_to(bulk_root))


def test_partial_resolve_keeps_symlinked_file(tmp_path: Path) -> None:
    bulk_root, save_dir = _setup_tree(tmp_path)

    torrent = _FakeTorrent(hash="HASHX", save_path=_qb_save_path(save_dir, bulk_root))
    files = [
        {"name": "movie.mkv", "size": 11_000},
        {"name": "movie.nfo", "size": 9},
    ]

    r = resolve(
        instance="qb",
        torrent=torrent,
        files=files,
        ssd_cache_dir=tmp_path / "ssd",
        path_map={"/data": str(bulk_root)},
        managed_paths=[bulk_root],
    )

    assert r is not None
    assert len(r.layouts) == 1
    assert r.layouts[0].link.name == "movie.mkv"
    assert r.total_bytes == 11_000  # nfo size is NOT counted


def test_returns_none_when_no_symlinks(tmp_path: Path) -> None:
    bulk_root = tmp_path / "mnt" / "media" / "storage"
    save_dir = bulk_root / "torrents" / "rel-Y"
    save_dir.mkdir(parents=True)
    (save_dir / "file.txt").write_text("real file, never linked")

    torrent = _FakeTorrent(hash="HASHY", save_path=_qb_save_path(save_dir, bulk_root))
    files = [{"name": "file.txt", "size": 22}]

    r = resolve(
        instance="qb",
        torrent=torrent,
        files=files,
        ssd_cache_dir=tmp_path / "ssd",
        path_map={"/data": str(bulk_root)},
        managed_paths=[bulk_root],
    )

    assert r is None


def test_managed_paths_filter_applies_per_file(tmp_path: Path) -> None:
    # A torrent whose movie.mkv resolves OUTSIDE managed_paths is filtered out.
    bulk_root = tmp_path / "mnt" / "media" / "storage"
    other_root = tmp_path / "somewhere-else"
    other_root.mkdir()
    (other_root / "movie.mkv").write_bytes(b"x" * 100)

    save_dir = bulk_root / "torrents" / "rel"
    save_dir.mkdir(parents=True)
    os.symlink(other_root / "movie.mkv", save_dir / "movie.mkv")

    torrent = _FakeTorrent(hash="HASHZ", save_path=_qb_save_path(save_dir, bulk_root))
    files = [{"name": "movie.mkv", "size": 100}]

    r = resolve(
        instance="qb",
        torrent=torrent,
        files=files,
        ssd_cache_dir=tmp_path / "ssd",
        path_map={"/data": str(bulk_root)},
        managed_paths=[bulk_root],
    )
    assert r is None
