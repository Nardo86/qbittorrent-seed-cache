"""Atomic symlink retargeting and copy helpers.

All public helpers are safe to call concurrently on different `link` paths,
but should be serialized per-link by the caller (the daemon holds a global
lock during a tick anyway).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
from collections.abc import Callable
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


PARTIAL_SUFFIX = ".qbsc-partial"
COPY_CHUNK_BYTES = 8 * 1024 * 1024
_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


class CopyInterrupted(Exception):
    """A stop was requested mid-copy. The partial file is kept for resume."""


def current_boot_id() -> str | None:
    """The kernel's per-boot id, or ``None`` where unavailable (non-Linux)."""
    try:
        return _BOOT_ID_PATH.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def partial_paths(dest: Path) -> tuple[Path, Path]:
    """``(partial file, its resume metadata)`` used while copying to ``dest``."""
    part = dest.with_name(f".{dest.name}{PARTIAL_SUFFIX}")
    return part, part.with_name(part.name + ".json")


def _resume_offset(part: Path, meta: Path, ident: dict[str, object]) -> int:
    """Bytes of ``part`` that can be trusted, or 0 to start over.

    A partial is resumable only if it was written for the very same source
    (path, size, mtime) **during the current boot**. Within one boot the
    page cache makes every byte a killed process wrote visible to the next
    one, so the prefix is exactly right. After a host crash/reboot the tail
    may not have reached the disk, so we start over rather than risk seeding
    a corrupt copy.
    """
    if ident.get("boot_id") is None:
        return 0
    try:
        with meta.open("r", encoding="utf-8") as fh:
            recorded = json.load(fh)
        size = part.stat().st_size
    except (OSError, ValueError):
        return 0
    if recorded != ident:
        return 0
    src_size = ident.get("size")
    if not isinstance(src_size, int) or size > src_size:
        return 0
    return size


def safe_copy(
    src: Path,
    dest: Path,
    *,
    stop: threading.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Copy file ``src`` → ``dest`` atomically and resumably.

    Data goes to a deterministic sibling ``.<name>.qbsc-partial`` that is
    fsynced and renamed over ``dest`` only once complete, so ``dest`` is
    either absent or whole. If the process is killed mid-copy, the partial
    (and a small ``.json`` describing source + boot) stays behind and the
    next call resumes from where it stopped instead of starting over (see
    :func:`_resume_offset`).

    ``stop`` is checked between chunks: when set, :class:`CopyInterrupted`
    is raised and the partial kept. ``on_progress(done, total)`` is called
    after every chunk. Any other error discards the partial.

    Returns the offset the copy resumed from (0 for a fresh copy).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part, meta = partial_paths(dest)
    st = os.stat(src)
    ident: dict[str, object] = {
        "src": str(src),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "boot_id": current_boot_id(),
    }
    offset = _resume_offset(part, meta, ident)
    if offset == 0:
        part.unlink(missing_ok=True)
        if ident["boot_id"] is None:
            meta.unlink(missing_ok=True)
        else:
            meta.write_text(json.dumps(ident), encoding="utf-8")
    try:
        with src.open("rb") as fin, part.open("r+b" if offset else "wb") as fout:
            with contextlib.suppress(AttributeError, OSError):
                os.posix_fadvise(fin.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
            fin.seek(offset)
            fout.seek(offset)
            fout.truncate(offset)
            done = offset
            while True:
                if stop is not None and stop.is_set():
                    raise CopyInterrupted(f"copy of {src} stopped at {done}/{st.st_size} bytes")
                buf = fin.read(COPY_CHUNK_BYTES)
                if not buf:
                    break
                fout.write(buf)
                done += len(buf)
                if on_progress is not None:
                    on_progress(done, st.st_size)
            fout.flush()
            os.fsync(fout.fileno())
        if done != st.st_size:
            raise OSError(f"{src} changed size during copy ({done} != {st.st_size})")
        shutil.copystat(src, part, follow_symlinks=True)
        os.replace(part, dest)
        meta.unlink(missing_ok=True)
    except CopyInterrupted:
        raise
    except Exception:
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise
    return offset


def remove_tree(path: Path) -> None:
    """rm -rf a directory or unlink a file. Idempotent."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def relative_target(link_dir: Path, target: Path) -> Path:
    """Compute the relative symlink target from a link's parent dir to `target`."""
    return Path(os.path.relpath(target, start=link_dir))
