"""Aggregation and dedup across multiple qB instances sharing the same infohash."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qbittorrent_seed_cache.mover import TorrentLayout, demote, promote
from qbittorrent_seed_cache.resolver import (
    ResolvedTorrent,
    aggregate,
)


def _layout(
    instance: str,
    infohash: str,
    link: Path,
    bulk_target: Path,
    ssd_target: Path,
) -> TorrentLayout:
    return TorrentLayout(
        instance=instance,
        infohash=infohash,
        link=link,
        bulk_target=bulk_target,
        ssd_target=ssd_target,
    )


def test_aggregate_groups_by_infohash() -> None:
    a = ResolvedTorrent(
        instance="qb",
        infohash="HASH-A",
        layouts=[_layout("qb", "HASH-A", Path("/q/l1"), Path("/b/a"), Path("/s/HASH-A/a"))],
        total_bytes=100,
    )
    b_qb = ResolvedTorrent(
        instance="qb",
        infohash="HASH-B",
        layouts=[_layout("qb", "HASH-B", Path("/q/l2"), Path("/b/b"), Path("/s/HASH-B/b"))],
        total_bytes=200,
    )
    b_af = ResolvedTorrent(
        instance="qbaf",
        infohash="HASH-B",
        layouts=[_layout("qbaf", "HASH-B", Path("/q2/l2"), Path("/b/b"), Path("/s/HASH-B/b"))],
        total_bytes=200,
    )

    logical = aggregate([a, b_qb, b_af])

    assert set(logical.keys()) == {"HASH-A", "HASH-B"}
    assert logical["HASH-A"].instances == ("qb",)
    assert logical["HASH-A"].size_bytes == 100
    assert logical["HASH-B"].instances == ("qb", "qbaf")
    # Layouts from both instances are concatenated.
    assert len(logical["HASH-B"].layouts) == 2
    # Size is NOT doubled — same infohash means same content.
    assert logical["HASH-B"].size_bytes == 200


def test_promote_dedups_ssd_copy_across_instances(tmp_path: Path) -> None:
    """One bulk file, two instances → one SSD copy, two retargeted symlinks."""
    bulk = tmp_path / "Films" / "X.mkv"
    bulk.parent.mkdir()
    bulk.write_bytes(b"payload" * 100)

    # Two instances' torrents/ dirs each containing a relative symlink to the
    # bulk file.
    inst_a = tmp_path / "qb-a" / "torrents" / "rel-A"
    inst_a.mkdir(parents=True)
    link_a = inst_a / "X.mkv"
    os.symlink(os.path.relpath(bulk, inst_a), link_a)

    inst_b = tmp_path / "qb-b" / "torrents" / "rel-B"
    inst_b.mkdir(parents=True)
    link_b = inst_b / "X.mkv"
    os.symlink(os.path.relpath(bulk, inst_b), link_b)

    ssd_target = tmp_path / "ssd" / "HASH" / "X.mkv"

    layouts = [
        _layout("a", "HASH", link_a, bulk, ssd_target),
        _layout("b", "HASH", link_b, bulk, ssd_target),
    ]

    bytes_copied = promote(layouts)

    # One SSD copy exists, with the right content.
    assert ssd_target.is_file()
    assert ssd_target.read_bytes() == bulk.read_bytes()
    # Total reported is just the size of the bulk file (no double-counting).
    assert bytes_copied == bulk.stat().st_size

    # Both links now point to the SSD copy.
    assert link_a.is_symlink()
    assert Path(os.readlink(link_a)) == ssd_target
    assert link_b.is_symlink()
    assert Path(os.readlink(link_b)) == ssd_target


def test_promote_idempotent_when_ssd_copy_exists(tmp_path: Path) -> None:
    bulk = tmp_path / "Films" / "X.mkv"
    bulk.parent.mkdir()
    bulk.write_bytes(b"original-content")

    inst = tmp_path / "qb" / "torrents" / "rel"
    inst.mkdir(parents=True)
    link = inst / "X.mkv"
    os.symlink(os.path.relpath(bulk, inst), link)

    ssd_target = tmp_path / "ssd" / "HASH" / "X.mkv"
    ssd_target.parent.mkdir(parents=True)
    ssd_target.write_bytes(b"original-content")  # same size — should be reused

    mtime_before = ssd_target.stat().st_mtime_ns

    promote([_layout("a", "HASH", link, bulk, ssd_target)])

    # File not rewritten.
    assert ssd_target.stat().st_mtime_ns == mtime_before
    # Link retargeted regardless.
    assert Path(os.readlink(link)) == ssd_target


def test_demote_retargets_all_instances_and_removes_ssd(tmp_path: Path) -> None:
    bulk = tmp_path / "Films" / "X.mkv"
    bulk.parent.mkdir()
    bulk.write_bytes(b"payload")

    inst_a = tmp_path / "qb-a" / "torrents" / "rel"
    inst_b = tmp_path / "qb-b" / "torrents" / "rel"
    inst_a.mkdir(parents=True)
    inst_b.mkdir(parents=True)

    ssd_target = tmp_path / "ssd" / "HASH" / "X.mkv"
    ssd_target.parent.mkdir(parents=True)
    ssd_target.write_bytes(b"payload")

    link_a = inst_a / "X.mkv"
    link_b = inst_b / "X.mkv"
    os.symlink(ssd_target, link_a)  # currently hot
    os.symlink(ssd_target, link_b)

    layouts = [
        _layout("a", "HASH", link_a, bulk, ssd_target),
        _layout("b", "HASH", link_b, bulk, ssd_target),
    ]

    freed = demote(layouts)

    assert freed == len(b"payload")
    # Both links retargeted (relative, into the bulk tree).
    assert Path(os.readlink(link_a)) == Path(os.path.relpath(bulk, inst_a))
    assert Path(os.readlink(link_b)) == Path(os.path.relpath(bulk, inst_b))
    # SSD <infohash>/ dir removed; <ssd_cache_dir> itself preserved.
    assert not ssd_target.parent.exists()
    assert ssd_target.parent.parent.exists()


def test_promote_dry_run_makes_no_changes(tmp_path: Path) -> None:
    bulk = tmp_path / "Films" / "X.mkv"
    bulk.parent.mkdir()
    bulk.write_bytes(b"payload")

    inst = tmp_path / "qb" / "torrents" / "rel"
    inst.mkdir(parents=True)
    link = inst / "X.mkv"
    os.symlink(os.path.relpath(bulk, inst), link)
    link_target_before = os.readlink(link)

    ssd_target = tmp_path / "ssd" / "HASH" / "X.mkv"

    promote([_layout("a", "HASH", link, bulk, ssd_target)], dry_run=True)

    assert not ssd_target.exists()
    assert os.readlink(link) == link_target_before


def test_promote_rejects_mixed_infohashes() -> None:
    layouts = [
        _layout("a", "HASH-A", Path("/x"), Path("/b"), Path("/s/HASH-A/x")),
        _layout("b", "HASH-B", Path("/x"), Path("/b"), Path("/s/HASH-B/x")),
    ]
    with pytest.raises(ValueError, match="single infohash"):
        promote(layouts, dry_run=True)
