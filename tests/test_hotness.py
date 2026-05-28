from __future__ import annotations

from qbittorrent_seed_cache.hotness import score
from qbittorrent_seed_cache.state import Snapshot

WINDOW = 14 * 86_400


def test_empty_history() -> None:
    s = score([], window_seconds=WINDOW)
    assert s.upload_bytes_in_window == 0
    assert s.upload_bytes_per_day == 0.0


def test_single_sample_is_cold() -> None:
    s = score([Snapshot(ts=1000, uploaded_session=500, upspeed=0)], window_seconds=WINDOW)
    assert s.upload_bytes_in_window == 0


def test_two_samples_simple_delta() -> None:
    # 1 GB uploaded in exactly 1 day → per_day = 1 GB.
    snaps = [
        Snapshot(ts=0, uploaded_session=0, upspeed=0),
        Snapshot(ts=86_400, uploaded_session=1_073_741_824, upspeed=0),
    ]
    s = score(snaps, window_seconds=WINDOW)
    assert s.upload_bytes_in_window == 1_073_741_824
    assert s.upload_bytes_per_day == 1_073_741_824.0


def test_reset_detection() -> None:
    # qB restarted between sample 1 and 2 — counter drops from 1000 to 200.
    # We treat the 200 as the post-restart delta.
    snaps = [
        Snapshot(ts=0, uploaded_session=1000, upspeed=0),
        Snapshot(ts=86_400, uploaded_session=200, upspeed=0),
    ]
    s = score(snaps, window_seconds=WINDOW)
    assert s.upload_bytes_in_window == 200


def test_last_activity_tracks_last_nonzero_delta() -> None:
    snaps = [
        Snapshot(ts=0, uploaded_session=0, upspeed=0),
        Snapshot(ts=100, uploaded_session=500, upspeed=5),
        Snapshot(ts=200, uploaded_session=500, upspeed=0),  # no delta
    ]
    s = score(snaps, window_seconds=WINDOW)
    assert s.last_activity_ts == 100
