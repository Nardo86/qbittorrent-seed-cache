from __future__ import annotations

from pathlib import Path

import pytest

from qbittorrent_seed_cache.paths import map_to_host


def test_no_match_returns_unchanged() -> None:
    assert map_to_host("/data/torrents/x", {}) == Path("/data/torrents/x")


def test_simple_prefix() -> None:
    pm = {"/data": "/mnt/media/storage"}
    assert map_to_host("/data/torrents/Film/X.mkv", pm) == Path(
        "/mnt/media/storage/torrents/Film/X.mkv"
    )


def test_longest_prefix_wins() -> None:
    pm = {"/data": "/host/short", "/data/torrents": "/host/long"}
    assert map_to_host("/data/torrents/X", pm) == Path("/host/long/X")
    assert map_to_host("/data/other/Y", pm) == Path("/host/short/other/Y")


def test_exact_prefix_match() -> None:
    pm = {"/data": "/mnt/x"}
    assert map_to_host("/data", pm) == Path("/mnt/x")


def test_partial_directory_name_is_not_a_match() -> None:
    pm = {"/data": "/mnt/x"}
    # /datacenter must NOT match /data (it's not a directory-boundary match).
    # PurePosixPath.relative_to enforces this.
    assert map_to_host("/datacenter/foo", pm) == Path("/datacenter/foo")


def test_rejects_relative_paths() -> None:
    with pytest.raises(ValueError):
        map_to_host("relative/path", {"/data": "/x"})
