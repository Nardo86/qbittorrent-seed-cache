"""Filesystem-side recovery metadata ("sidecar") and anomaly signalling.

Every promoted (hot) torrent gets a ``.qbsc-meta.json`` sidecar written
*inside* its ``<ssd_cache_dir>/<infohash>/`` directory. The sidecar records
everything needed to rebuild that torrent's DB tier row from the filesystem
alone::

    {
      "schema_version": 1,
      "infohash": "abc123...",
      "since_ts": 1700000000,
      "ssd_bytes": 12345678,
      "bulk_targets": {"<symlink path>": "<bulk file path>", ...}
    }

Why this exists
---------------
The SSD cache is disposable — the canonical file always lives in the bulk
filesystem. What is *not* disposable is the ``link -> bulk`` mapping: once a
torrent is promoted, its symlink points into the SSD, so ``readlink`` can no
longer tell us where the original bulk file was. The DB normally holds that
mapping (``tier.bulk_targets``), but if the DB is lost, corrupted, or
replaced — e.g. when migrating the daemon to a new deployment — the mapping
vanishes and the daemon loses track of already-promoted content. It then
sees free quota that is not really free and over-promotes on top of the
existing cache, filling the disk. (This is exactly the incident that
motivated this module.)

The sidecar makes the filesystem self-describing, so the mapping survives a
wiped DB: :mod:`qbittorrent_seed_cache.reconcile` rebuilds the tier rows from
the sidecars at startup.

Anomaly marker
--------------
When the tick finds a live symlink that resolves into the SSD but whose
``link -> bulk`` mapping is in neither the DB nor a sidecar, it drops an
anomaly marker file in the SSD cache dir (see
:mod:`qbittorrent_seed_cache.anomaly`). The marker lists the affected links
and is surfaced by the healthcheck output, turning a silent data-integrity
problem into a visible one that an operator can act on (``repair-dangling``).

Heartbeat
---------
The daemon refreshes ``.qbsc-heartbeat`` at every tick and while a long
promotion copy is in progress. The healthcheck uses it as a *liveness*
signal: a stale heartbeat means the daemon is hung, which a restart can fix
(unlike a data anomaly, which it cannot).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

META_NAME = ".qbsc-meta.json"
META_SCHEMA_VERSION = 1
ANOMALY_MARKER = ".qbsc-anomaly"
HEARTBEAT_NAME = ".qbsc-heartbeat"

# Bookkeeping files that live inside an ``<infohash>/`` dir but are not
# cached payload.
_INTERNAL_NAMES = frozenset({META_NAME})


@dataclass(frozen=True, slots=True)
class SidecarMeta:
    """Decoded contents of a ``.qbsc-meta.json`` sidecar."""

    infohash: str
    since_ts: int
    ssd_bytes: int
    bulk_targets: dict[str, str]


def meta_path(ssd_cache_dir: Path, infohash: str) -> Path:
    """Path of the sidecar for ``infohash`` under ``ssd_cache_dir``."""
    return ssd_cache_dir / infohash / META_NAME


def _atomic_write_json(dest: Path, payload: object, *, fsync: bool) -> None:
    """Write ``payload`` as JSON to ``dest`` via a sibling tmp file + rename."""
    fd, tmp_name = tempfile.mkstemp(prefix=f"{dest.name}.", dir=str(dest.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def write_meta(
    ssd_cache_dir: Path,
    *,
    infohash: str,
    since_ts: int,
    ssd_bytes: int,
    bulk_targets: dict[str, str],
) -> None:
    """Atomically (tmp + rename) write the recovery sidecar for ``infohash``.

    The infohash directory is expected to already exist (the SSD copies are
    written into it before this is called); we create it defensively anyway.
    """
    dest = meta_path(ssd_cache_dir, infohash)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": META_SCHEMA_VERSION,
        "infohash": infohash,
        "since_ts": since_ts,
        "ssd_bytes": ssd_bytes,
        "bulk_targets": bulk_targets,
    }
    _atomic_write_json(dest, payload, fsync=True)


def read_meta(ssd_cache_dir: Path, infohash: str) -> SidecarMeta | None:
    """Return the decoded sidecar for ``infohash``, or ``None``.

    ``None`` is returned when the sidecar is absent or unreadable/corrupt.
    Corrupt sidecars are logged (they indicate a partially-written file or
    on-disk damage) but never raise — the caller treats a ``None`` as "no
    recovery info available".
    """
    path = meta_path(ssd_cache_dir, infohash)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("recovery.meta_unreadable", path=str(path), error=str(exc))
        return None

    version = data.get("schema_version")
    if version != META_SCHEMA_VERSION:
        log.error("recovery.meta_bad_version", path=str(path), version=version)
        return None
    bulk_targets = data.get("bulk_targets")
    if not isinstance(bulk_targets, dict) or not bulk_targets:
        log.error("recovery.meta_no_bulk_targets", path=str(path))
        return None

    try:
        since_ts = int(data["since_ts"])
        ssd_bytes = int(data["ssd_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        log.error("recovery.meta_bad_fields", path=str(path), error=str(exc))
        return None

    return SidecarMeta(
        infohash=str(data.get("infohash", infohash)),
        since_ts=since_ts,
        ssd_bytes=ssd_bytes,
        # Coerce to a plain str->str dict.
        bulk_targets={str(k): str(v) for k, v in bulk_targets.items()},
    )


def iter_ssd_infohash_dirs(ssd_cache_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(infohash, dir)`` for every SSD cache subdirectory.

    A torrent's cache lives at ``<ssd_cache_dir>/<infohash>/``. Dotfiles
    (the ``.ssd-mount-ok`` marker, the anomaly marker) and stray files are
    skipped — only directories are real cache entries.
    """
    if not ssd_cache_dir.is_dir():
        return
    for entry in sorted(ssd_cache_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir():
            continue
        yield entry.name, entry


def ssd_dir_has_payload(ssd_dir: Path) -> bool:
    """True if the cache dir holds at least one real (non-sidecar) file."""
    if not ssd_dir.is_dir():
        return False
    for root, _dirs, files in os.walk(ssd_dir):
        for name in files:
            if name in _INTERNAL_NAMES:
                continue
            full = Path(root) / name
            try:
                if full.stat().st_size > 0:
                    return True
            except OSError:
                continue
    return False


def ssd_dir_bytes(ssd_dir: Path) -> int:
    """Sum of real (non-sidecar) file sizes in the cache dir."""
    total = 0
    if not ssd_dir.is_dir():
        return 0
    for root, _dirs, files in os.walk(ssd_dir):
        for name in files:
            if name in _INTERNAL_NAMES:
                continue
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def set_anomaly(ssd_cache_dir: Path, detail: str) -> None:
    """Create/refresh the anomaly marker (reported by the healthcheck)."""
    marker = ssd_cache_dir / ANOMALY_MARKER
    try:
        marker.write_text(detail, encoding="utf-8")
    except OSError as exc:
        log.error("recovery.anomaly_marker_write_failed", error=str(exc))


def clear_anomaly(ssd_cache_dir: Path) -> None:
    """Remove the anomaly marker if present (state is clean)."""
    marker = ssd_cache_dir / ANOMALY_MARKER
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        log.error("recovery.anomaly_marker_clear_failed", error=str(exc))


def has_anomaly(ssd_cache_dir: Path) -> bool:
    return (ssd_cache_dir / ANOMALY_MARKER).exists()


def read_anomaly(ssd_cache_dir: Path) -> str | None:
    """Return the anomaly marker's text, or ``None`` if there is no marker."""
    marker = ssd_cache_dir / ANOMALY_MARKER
    try:
        return marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"(anomaly marker unreadable: {exc})"


# --- heartbeat --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Heartbeat:
    ts: int
    stale_after_ts: int


def write_heartbeat(ssd_cache_dir: Path, *, now_ts: int, stale_after_ts: int) -> None:
    """Record that the daemon is alive and when that claim expires.

    The daemon decides the deadline (it knows its poll interval); the
    healthcheck only compares it with the clock. Best effort: a failure is
    logged, never raised — a missed beat must not break a tick.
    """
    payload = {"ts": now_ts, "stale_after_ts": stale_after_ts, "pid": os.getpid()}
    try:
        _atomic_write_json(ssd_cache_dir / HEARTBEAT_NAME, payload, fsync=False)
    except OSError as exc:
        log.warning("recovery.heartbeat_write_failed", error=str(exc))


def read_heartbeat(ssd_cache_dir: Path) -> Heartbeat | None:
    """Return the last heartbeat, or ``None`` if absent or unreadable."""
    try:
        with (ssd_cache_dir / HEARTBEAT_NAME).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return Heartbeat(ts=int(data["ts"]), stale_after_ts=int(data["stale_after_ts"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None

