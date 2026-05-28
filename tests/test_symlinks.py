"""Smoke tests for symlink primitives.

The promote/demote logic is essentially "atomically retarget a symlink",
so getting these primitives right is load-bearing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qbittorrent_seed_cache.symlinks import (
    atomic_retarget,
    relative_target,
    remove_tree,
    safe_copy,
)


def test_atomic_retarget_swaps_target(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    link = tmp_path / "link"
    a.write_text("aaa")
    b.write_text("bbb")
    os.symlink(a, link)
    assert link.read_text() == "aaa"

    atomic_retarget(link, b)
    assert link.is_symlink()
    assert link.read_text() == "bbb"
    assert os.readlink(link) == str(b)


def test_atomic_retarget_refuses_real_file(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("real")
    with pytest.raises(ValueError, match="not a symlink"):
        atomic_retarget(real, tmp_path / "other")


def test_safe_copy_creates_parent(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"payload")
    dest = tmp_path / "nested" / "deep" / "dest"

    safe_copy(src, dest)
    assert dest.read_bytes() == b"payload"


def test_safe_copy_atomic_leaves_no_tmp_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"data")
    dest = tmp_path / "dest"

    import shutil as _shutil

    def boom(*_a: object, **_kw: object) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(_shutil, "copyfile", boom)
    with pytest.raises(OSError, match="simulated"):
        safe_copy(src, dest)

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".dest.qbsc-")]
    assert leftovers == [], f"left tmp files behind: {leftovers}"
    assert not dest.exists()


def test_remove_tree_handles_missing(tmp_path: Path) -> None:
    remove_tree(tmp_path / "does-not-exist")  # must not raise


def test_relative_target_simple(tmp_path: Path) -> None:
    link_dir = tmp_path / "torrents" / "release"
    link_dir.mkdir(parents=True)
    real = tmp_path / "Films" / "X.mkv"
    real.parent.mkdir()
    real.write_text("x")
    rel = relative_target(link_dir, real)
    assert str(rel) == os.path.join("..", "..", "Films", "X.mkv")
