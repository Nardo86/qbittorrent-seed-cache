"""Atomic symlink retargeting and copy helpers.

All public helpers are safe to call concurrently on different `link` paths,
but should be serialized per-link by the caller (the daemon holds a global
lock during a tick anyway).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def atomic_retarget(link: Path, new_target: Path) -> None:
    """Atomically point `link` to `new_target` via tmp + rename.

    `link` must already exist and be a symlink. We create a sibling symlink
    with a temporary name and `os.replace()` it over `link`. On POSIX this
    is atomic — readers either see the old or the new symlink, never a
    missing entry.
    """
    if not link.is_symlink():
        raise ValueError(f"{link} is not a symlink (refusing to clobber a real file)")

    tmp = link.with_name(f".{link.name}.qbsc-tmp-{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(new_target, tmp)
    os.replace(tmp, link)


def safe_copy(src: Path, dest: Path) -> None:
    """Copy file src→dest atomically (tmp file + rename within dest dir)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.qbsc-", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        shutil.copystat(src, tmp, follow_symlinks=True)
        os.replace(tmp, dest)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def remove_tree(path: Path) -> None:
    """rm -rf a directory or unlink a file. Idempotent."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def relative_target(link_dir: Path, target: Path) -> Path:
    """Compute the relative symlink target from a link's parent dir to `target`."""
    return Path(os.path.relpath(target, start=link_dir))
