"""SQLite-backed rolling-window upload state.

Snapshot metrics are stored per (instance, infohash) — each qB instance has
its own counters that may reset independently. Tier state is *logical*,
keyed by infohash only: the SSD copy exists at most once per infohash and
is shared by all instances seeding that infohash.

uploaded_session resets when qB restarts. We detect resets (current < previous)
and treat the new value as the delta from zero. The rolling-window score
is computed in hotness.py from the deltas between consecutive snapshots.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    instance         TEXT    NOT NULL,
    infohash         TEXT    NOT NULL,
    ts               INTEGER NOT NULL,
    uploaded_session INTEGER NOT NULL,
    upspeed          INTEGER NOT NULL,
    PRIMARY KEY (instance, infohash, ts)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_recent
    ON snapshots (instance, infohash, ts DESC);

CREATE TABLE IF NOT EXISTS tier (
    infohash  TEXT    NOT NULL PRIMARY KEY,
    tier      TEXT    NOT NULL CHECK (tier IN ('cold','hot')),
    since_ts  INTEGER NOT NULL,
    ssd_bytes INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    ts: int
    uploaded_session: int
    upspeed: int


class StateStore:
    """Thin sync wrapper. Daemon code runs it via asyncio.to_thread."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the daemon serializes SQLite access at the
        # event loop level but dispatches some queries through asyncio.to_thread,
        # so the underlying connection is touched from the executor thread pool.
        self._conn = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

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

    def get_tier(self, *, infohash: str) -> tuple[str, int] | None:
        row = self._conn.execute(
            "SELECT tier, since_ts FROM tier WHERE infohash = ?",
            (infohash,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def set_tier(
        self, *, infohash: str, tier: str, since_ts: int, ssd_bytes: int = 0
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO tier (infohash, tier, since_ts, ssd_bytes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(infohash) DO UPDATE SET
                tier=excluded.tier,
                since_ts=excluded.since_ts,
                ssd_bytes=excluded.ssd_bytes
            """,
            (infohash, tier, since_ts, ssd_bytes),
        )

    def delete_tier(self, *, infohash: str) -> None:
        self._conn.execute("DELETE FROM tier WHERE infohash = ?", (infohash,))

    def hot_infohashes(self) -> list[str]:
        cur = self._conn.execute("SELECT infohash FROM tier WHERE tier = 'hot'")
        return [row[0] for row in cur.fetchall()]

    def hot_total_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(ssd_bytes), 0) FROM tier WHERE tier = 'hot'"
        ).fetchone()
        return int(row[0])
