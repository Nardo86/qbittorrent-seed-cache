"""SQLite-backed rolling-window upload state.

Snapshot metrics are stored per (instance, infohash) — each qB instance has
its own counters that may reset independently. Tier state is *logical*,
keyed by infohash only: the SSD copy exists at most once per infohash and
is shared by all instances seeding that infohash.

The tier row for a hot torrent also persists its `bulk_targets` (a map from
each symlink path to the canonical bulk-fs file it backed before promotion).
Without this, the next tick's resolver would see the symlink pointing into
the SSD and lose track of the bulk file — leading to the torrent being
classified as gone-from-qB and orphan-cleaned. See resolver.resolve().

uploaded_session resets when qB restarts. We detect resets (current < previous)
and treat the new value as the delta from zero. The rolling-window score
is computed in hotness.py from the deltas between consecutive snapshots.

Size: one row per (instance, torrent) per poll adds up — with a 60 s poll,
~400 torrents and a 14-day window that is ~8M rows (≈1-2 GB). Rows older than
the window are pruned every tick; on top of that `compact_snapshots` thins
rows older than a day to one per bucket in a way that leaves the hotness
sums unchanged, and `vacuum_if_fragmented` gives the space freed by pruning
back to the filesystem (SQLite never shrinks the file on its own).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    instance         TEXT    NOT NULL,
    infohash         TEXT    NOT NULL,
    ts               INTEGER NOT NULL,
    uploaded_session INTEGER NOT NULL,
    upspeed          INTEGER NOT NULL,
    PRIMARY KEY (instance, infohash, ts)
);

CREATE TABLE IF NOT EXISTS tier (
    infohash      TEXT    NOT NULL PRIMARY KEY,
    tier          TEXT    NOT NULL CHECK (tier IN ('cold','hot')),
    since_ts      INTEGER NOT NULL,
    ssd_bytes     INTEGER NOT NULL DEFAULT 0,
    bulk_targets  TEXT
);
"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    ts: int
    uploaded_session: int
    upspeed: int


@dataclass(frozen=True, slots=True)
class TierRow:
    tier: str
    since_ts: int
    bulk_targets: dict[str, str] | None


class StateStore:
    """Thin sync wrapper. Daemon code runs it via asyncio.to_thread."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self._path = path
        if readonly:
            # For tools running next to the daemon (repair-dangling): no
            # schema/migration writes, no risk of touching the daemon's DB.
            self._conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
            )
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the daemon serializes SQLite access at the
        # event loop level but dispatches some queries through asyncio.to_thread,
        # so the underlying connection is touched from the executor thread pool.
        self._conn = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate_bulk_targets()
        # Redundant with the primary key's index (same columns; SQLite scans
        # it in either direction) — it only doubled the snapshot footprint.
        self._conn.execute("DROP INDEX IF EXISTS idx_snapshots_recent")

    def _migrate_bulk_targets(self) -> None:
        """Add tier.bulk_targets to pre-existing DBs that didn't have it."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tier)")}
        if "bulk_targets" not in cols:
            self._conn.execute("ALTER TABLE tier ADD COLUMN bulk_targets TEXT")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def record(
        self, *, instance: str, infohash: str, ts: int, uploaded_session: int, upspeed: int
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)",
            (instance, infohash, ts, uploaded_session, upspeed),
        )

    def history(
        self, *, instance: str, infohash: str, since_ts: int
    ) -> list[Snapshot]:
        cur = self._conn.execute(
            """
            SELECT ts, uploaded_session, upspeed
              FROM snapshots
             WHERE instance = ? AND infohash = ? AND ts >= ?
             ORDER BY ts ASC
            """,
            (instance, infohash, since_ts),
        )
        return [Snapshot(*row) for row in cur.fetchall()]

    def prune(self, *, before_ts: int) -> int:
        cur = self._conn.execute("DELETE FROM snapshots WHERE ts < ?", (before_ts,))
        return cur.rowcount or 0

    def compact_snapshots(self, *, before_ts: int, bucket_seconds: int) -> int:
        """Thin snapshots older than ``before_ts`` to ~one per ``bucket_seconds``.

        The hotness score only sums positive deltas of ``uploaded_session``
        (with a qB restart, i.e. a value lower than the previous one, counting
        the new value from zero). Inside a run without restarts the counter is
        non-decreasing and the deltas telescope, so interior rows can go
        without changing the sum. We keep, per (instance, infohash) series:

        * the first row and the last old row;
        * the last row of every time bucket (resolution of the thinned part);
        * both rows around every restart (``next < current``).

        Every removed row then lies strictly inside a non-decreasing run, so
        the summed upload over any range of kept rows is exactly what it was.
        (Only a window boundary falling inside a thinned stretch is affected,
        by at most one bucket out of the whole window.)

        Works series by series to keep memory bounded. Returns rows deleted.
        """
        series = self._conn.execute(
            "SELECT DISTINCT instance, infohash FROM snapshots WHERE ts < ?", (before_ts,)
        ).fetchall()
        deleted = 0
        for instance, infohash in series:
            rows = self._conn.execute(
                """
                SELECT ts, uploaded_session FROM snapshots
                 WHERE instance = ? AND infohash = ? AND ts < ?
                 ORDER BY ts ASC
                """,
                (instance, infohash, before_ts),
            ).fetchall()
            drop = _thin(rows, bucket_seconds)
            if not drop:
                continue
            with self._tx() as conn:
                conn.executemany(
                    "DELETE FROM snapshots WHERE instance = ? AND infohash = ? AND ts = ?",
                    [(instance, infohash, ts) for ts in drop],
                )
            deleted += len(drop)
        return deleted

    def file_stats(self) -> tuple[int, int]:
        """``(file bytes, free-page bytes)`` of the main database file."""
        page_size = int(self._conn.execute("PRAGMA page_size").fetchone()[0])
        pages = int(self._conn.execute("PRAGMA page_count").fetchone()[0])
        free = int(self._conn.execute("PRAGMA freelist_count").fetchone()[0])
        return pages * page_size, free * page_size

    def vacuum_if_fragmented(
        self, *, min_free_ratio: float = 0.5, min_free_bytes: int = 32 * 1024 * 1024
    ) -> bool:
        """VACUUM when most of the file is free pages (e.g. after pruning).

        Deleting rows only moves pages to SQLite's freelist; the file never
        shrinks. Rewriting it costs about the size of the *live* data, so this
        is cheap exactly when it is worth doing. Returns True if it ran.
        """
        size, free = self.file_stats()
        if free < min_free_bytes or free < size * min_free_ratio:
            return False
        self._conn.execute("VACUUM")
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True

    def get_tier(self, *, infohash: str) -> TierRow | None:
        row = self._conn.execute(
            "SELECT tier, since_ts, bulk_targets FROM tier WHERE infohash = ?",
            (infohash,),
        ).fetchone()
        if row is None:
            return None
        bulk_targets = json.loads(row[2]) if row[2] else None
        return TierRow(tier=row[0], since_ts=row[1], bulk_targets=bulk_targets)

    def set_tier(
        self,
        *,
        infohash: str,
        tier: str,
        since_ts: int,
        ssd_bytes: int = 0,
        bulk_targets: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(bulk_targets) if bulk_targets is not None else None
        self._conn.execute(
            """
            INSERT INTO tier (infohash, tier, since_ts, ssd_bytes, bulk_targets)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(infohash) DO UPDATE SET
                tier=excluded.tier,
                since_ts=excluded.since_ts,
                ssd_bytes=excluded.ssd_bytes,
                bulk_targets=excluded.bulk_targets
            """,
            (infohash, tier, since_ts, ssd_bytes, encoded),
        )

    def delete_tier(self, *, infohash: str) -> None:
        self._conn.execute("DELETE FROM tier WHERE infohash = ?", (infohash,))

    def hot_infohashes(self) -> list[str]:
        cur = self._conn.execute("SELECT infohash FROM tier WHERE tier = 'hot'")
        return [row[0] for row in cur.fetchall()]

    def hot_bulk_maps(self) -> dict[str, dict[str, str]]:
        """Return {infohash: {link_path: bulk_path}} for every hot torrent
        with a persisted bulk_targets map. Hot rows without bulk_targets are
        omitted (legacy / corrupt state — the caller treats them as unknown)."""
        cur = self._conn.execute(
            "SELECT infohash, bulk_targets FROM tier WHERE tier = 'hot' AND bulk_targets IS NOT NULL"
        )
        return {row[0]: json.loads(row[1]) for row in cur.fetchall()}

    def hot_total_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(ssd_bytes), 0) FROM tier WHERE tier = 'hot'"
        ).fetchone()
        return int(row[0])


def _thin(rows: Sequence[tuple[int, int]], bucket_seconds: int) -> list[int]:
    """Timestamps of the rows of one series that `compact_snapshots` may drop.

    ``rows`` are ``(ts, uploaded_session)`` sorted by ts.
    """
    n = len(rows)
    if n <= 2:
        return []
    keep = [False] * n
    keep[0] = keep[-1] = True
    for i in range(n - 1):
        ts, up = rows[i]
        nts, nup = rows[i + 1]
        if nup < up:  # counter reset between i and i+1: keep both sides
            keep[i] = keep[i + 1] = True
        if nts // bucket_seconds != ts // bucket_seconds:  # last row of its bucket
            keep[i] = True
    return [rows[i][0] for i in range(n) if not keep[i]]
