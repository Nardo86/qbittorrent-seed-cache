"""Detection and reporting of symlinks into the SSD with a lost mapping.

The unrecoverable state this module watches for: a live qB symlink resolves
into ``<ssd_cache_dir>/`` (its target may even be gone — a dangling link),
but its ``link -> bulk`` mapping is in neither the DB tier row nor the
on-disk sidecar. The daemon can neither demote such a link (it does not know
where the bulk file is) nor safely touch the torrent's SSD dir.

What a restart can and cannot fix
---------------------------------
This is a *data* problem, not a *liveness* problem: restarting the daemon
does not bring the mapping back. It used to turn the container unhealthy,
and with an ``autoheal``-style sidecar that meant a kill/restart every few
minutes — each restart killing an in-flight promotion copy, which the next
start then reclaimed and copied again from scratch, forever. So:

* the anomaly is reported through the marker file (shown by the
  healthcheck), and an ``error`` log line **only when the set of affected
  links changes or once per** ``anomaly_log_interval_sec`` — not once per
  file per tick;
* the affected torrents are **quarantined** by the daemon (never promoted,
  demoted or displaced) until repaired, and their untracked SSD bytes count
  against the quota;
* the healthcheck stays healthy unless explicitly told otherwise
  (``QBSC_HEALTHCHECK_FAIL_ON_ANOMALY=1``);
* ``qbittorrent-seed-cache repair-dangling`` diagnoses (and, with
  ``--apply``, repairs) the links.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Set
from pathlib import Path

import structlog

from . import recovery
from .resolver import is_under
from .state import StateStore

log = structlog.get_logger(__name__)

REPAIR_HINT = (
    "diagnose with `qbittorrent-seed-cache repair-dangling` (read-only), "
    "then repair verified links with `--apply`"
)

_FIRST_SEEN_PREFIX = "first_seen: "


def find_unmapped_links(
    ssd_cache_dir: Path,
    store: StateStore,
    ssd_links: Mapping[str, Set[str]],
) -> dict[str, list[str]]:
    """Return ``{infohash: [link, ...]}`` for SSD links with no known mapping.

    ``ssd_links`` is the tick's live view (links that resolved into the SSD
    when qB was polled). A link counts as *mapped* when it appears in the
    torrent's hot DB row or in its sidecar. Each candidate link is re-checked
    against the filesystem so a link retargeted earlier in the same tick is
    not reported.
    """
    hot_maps = store.hot_bulk_maps()
    out: dict[str, list[str]] = {}
    for infohash in sorted(ssd_links):
        links = ssd_links[infohash]
        known: set[str] = set(hot_maps.get(infohash, {}))
        if not links <= known:
            meta = recovery.read_meta(ssd_cache_dir, infohash)
            if meta is not None:
                known |= set(meta.bulk_targets)
        missing = sorted(
            link
            for link in links
            if link not in known
            and os.path.islink(link)
            and is_under(Path(os.path.realpath(link)), ssd_cache_dir)
        )
        if missing:
            out[infohash] = missing
    return out


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _read_first_seen(ssd_cache_dir: Path) -> str | None:
    text = recovery.read_anomaly(ssd_cache_dir)
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith(_FIRST_SEEN_PREFIX):
            return line[len(_FIRST_SEEN_PREFIX):].strip() or None
    return None


def format_marker(unmapped: Mapping[str, list[str]], *, first_seen: str, updated: str) -> str:
    """Human-readable marker body. The first line is the one-line summary."""
    n_links = sum(len(v) for v in unmapped.values())
    lines = [
        f"{len(unmapped)} torrent(s), {n_links} symlink(s) point into the SSD "
        "with no recoverable link->bulk mapping",
        f"{_FIRST_SEEN_PREFIX}{first_seen}",
        f"updated: {updated}",
        "Restarting the daemon does not fix this. The affected torrents are "
        "quarantined (never promoted/demoted) until repaired.",
        f"Next step: {REPAIR_HINT}.",
        "",
    ]
    for infohash in sorted(unmapped):
        lines.extend(f"{infohash}\t{link}" for link in unmapped[infohash])
    return "\n".join(lines) + "\n"


class AnomalyReporter:
    """Owns the anomaly marker and rate-limits the anomaly log line.

    One instance lives for the daemon's lifetime. The marker is rewritten
    only when the affected set changes (or the marker went missing); the
    ``error`` line is emitted on change and then at most once per
    ``log_interval_sec`` while the anomaly persists.
    """

    def __init__(self, *, log_interval_sec: int) -> None:
        self._log_interval_sec = log_interval_sec
        self._last_key: frozenset[tuple[str, str]] | None = None
        self._last_log_ts: float = 0.0
        self._first_seen: str | None = None

    def report(
        self,
        ssd_cache_dir: Path,
        unmapped: Mapping[str, list[str]],
        *,
        now_ts: float,
        dry_run: bool,
    ) -> None:
        key = frozenset((ih, link) for ih, links in unmapped.items() for link in links)

        if not key:
            if self._last_key or recovery.has_anomaly(ssd_cache_dir):
                log.info("tick.anomaly_cleared")
                if not dry_run:
                    recovery.clear_anomaly(ssd_cache_dir)
            self._last_key = key
            self._first_seen = None
            return

        changed = key != self._last_key
        if self._first_seen is None:
            # Survive restarts: keep the original first_seen from the marker.
            self._first_seen = _read_first_seen(ssd_cache_dir) or _iso(now_ts)
        if not dry_run and (changed or not recovery.has_anomaly(ssd_cache_dir)):
            recovery.set_anomaly(
                ssd_cache_dir,
                format_marker(unmapped, first_seen=self._first_seen, updated=_iso(now_ts)),
            )

        if changed or now_ts - self._last_log_ts >= self._log_interval_sec:
            log.error(
                "tick.anomaly_present",
                torrents=len(unmapped),
                links=len(key),
                infohashes=sorted(unmapped)[:20],
                changed=changed,
                first_seen=self._first_seen,
                marker=str(ssd_cache_dir / recovery.ANOMALY_MARKER),
                hint=REPAIR_HINT,
            )
            self._last_log_ts = now_ts
        else:
            log.debug("tick.anomaly_present", torrents=len(unmapped), links=len(key))
        self._last_key = key
