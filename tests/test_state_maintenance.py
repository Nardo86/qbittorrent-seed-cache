"""state.db growth: index cleanup, exact snapshot thinning, VACUUM.

Production: a 60 s poll over ~390 torrents x 2 instances kept ~8M rows in the
14-day window; the file reached 1.3 GB and, after a large prune, stayed there
with 97% free pages (SQLite never shrinks a file without VACUUM).
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from qbittorrent_seed_cache.hotness import score
from qbittorrent_seed_cache.state import Snapshot, StateStore, _thin

DAY = 86_400


def _series(n: int, step: int, seed: int, resets: int = 3) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    reset_at = set(rng.sample(range(1, n), resets))
    up, out = 0, []
    for i in range(n):
        if i in reset_at:
            up = rng.randint(0, 5_000)  # qB restarted: counter from ~0
        else:
            up += rng.choice([0, 0, 0, rng.randint(1, 10_000_000)])
        out.append((1_000_000 + i * step, up))
    return out


def _total(rows: list[tuple[int, int]]) -> int:
    snaps = [Snapshot(ts=t, uploaded_session=u, upspeed=0) for t, u in rows]
    return score(snaps, window_seconds=14 * DAY).upload_bytes_in_window


def test_thin_preserves_upload_sum_exactly() -> None:
    for seed in range(50):
        rows = _series(3000, 60, seed)
        drop = set(_thin(rows, 3600))
        kept = [r for r in rows if r[0] not in drop]
        assert len(kept) < len(rows) / 10
        assert _total(kept) == _total(rows), seed
        # Idempotent.
        assert _thin(kept, 3600) == []


def test_thin_keeps_both_sides_of_a_reset() -> None:
    rows = [(0, 10), (60, 20), (120, 30), (180, 5), (240, 6), (300, 7)]
    drop = _thin(rows, 10_000)  # one big bucket
    assert drop == [60, 240]
    kept = [r for r in rows if r[0] not in drop]
    assert _total(kept) == _total(rows) == 20 + 5 + 2


def test_compact_snapshots_on_store(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.db")
    try:
        rows = _series(3 * 1440, 60, seed=7)  # 3 days at 60 s
        for ts, up in rows:
            store.record(instance="qb", infohash="H", ts=ts, uploaded_session=up, upspeed=0)
        before_ts = rows[-1][0] - DAY
        full = store.history(instance="qb", infohash="H", since_ts=0)

        deleted = store.compact_snapshots(before_ts=before_ts, bucket_seconds=3600)
        after = store.history(instance="qb", infohash="H", since_ts=0)
        assert deleted == len(full) - len(after) and deleted > 2000
        # Last day untouched.
        assert [s for s in after if s.ts >= before_ts] == [s for s in full if s.ts >= before_ts]
        assert score(after, 14 * DAY).upload_bytes_in_window == score(
            full, 14 * DAY
        ).upload_bytes_in_window
        assert store.compact_snapshots(before_ts=before_ts, bucket_seconds=3600) == 0
    finally:
        store.close()


def test_vacuum_after_prune_shrinks_file(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.db")
    try:
        for i in range(60_000):
            store.record(instance="qb", infohash=f"H{i % 300}", ts=i, uploaded_session=i, upspeed=0)
        assert store.vacuum_if_fragmented(min_free_bytes=0) is False  # nothing free yet
        store.prune(before_ts=59_000)
        size, free = store.file_stats()
        assert free > size / 2
        assert store.vacuum_if_fragmented(min_free_bytes=0) is True
        size2, free2 = store.file_stats()
        assert size2 < size / 4 and free2 == 0
        assert store.vacuum_if_fragmented(min_free_bytes=0) is False
    finally:
        store.close()


def test_redundant_index_dropped_and_history_uses_pk(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE snapshots (instance TEXT NOT NULL, infohash TEXT NOT NULL,
            ts INTEGER NOT NULL, uploaded_session INTEGER NOT NULL,
            upspeed INTEGER NOT NULL, PRIMARY KEY (instance, infohash, ts));
        CREATE INDEX idx_snapshots_recent ON snapshots (instance, infohash, ts DESC);
        """
    )
    conn.close()
    store = StateStore(db)
    try:
        conn = sqlite3.connect(db)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_snapshots_recent" not in names
        plan = " ".join(
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT ts, uploaded_session, upspeed FROM snapshots "
                "WHERE instance = ? AND infohash = ? AND ts >= ? ORDER BY ts ASC",
                ("a", "b", 0),
            )
        )
        conn.close()
        assert "sqlite_autoindex_snapshots_1" in plan
        assert "TEMP B-TREE" not in plan
    finally:
        store.close()
