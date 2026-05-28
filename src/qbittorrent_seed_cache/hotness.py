"""Hotness scoring from snapshot history.

We compute upload bytes per day over a rolling window. Deltas are derived
from consecutive `uploaded_session` snapshots, with reset detection: if a
new value is *lower* than the previous one (qB restarted) we treat the
new value as the delta from zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from .state import Snapshot


@dataclass(frozen=True, slots=True)
class HotnessScore:
    upload_bytes_in_window: int
    upload_bytes_per_day: float
    last_activity_ts: int


def score(snapshots: list[Snapshot], window_seconds: int) -> HotnessScore:
    """Compute hotness from snapshots ordered by ts ASC.

    `snapshots` is expected to already be filtered to the relevant time window
    by the caller (StateStore.history with since_ts).
    """
    if len(snapshots) < 2:
        # Not enough data yet — treat as cold but record any current upspeed.
        last_ts = snapshots[-1].ts if snapshots else 0
        return HotnessScore(0, 0.0, last_ts)

    total = 0
    last_activity = snapshots[0].ts
    for prev, cur in pairwise(snapshots):
        delta = cur.uploaded_session - prev.uploaded_session
        if delta < 0:
            # qB restart: prev counter discarded, current value is the delta
            delta = cur.uploaded_session
        if delta > 0:
            total += delta
            last_activity = cur.ts

    span = snapshots[-1].ts - snapshots[0].ts
    if span <= 0:
        return HotnessScore(total, 0.0, last_activity)

    per_day = total / span * 86_400.0
    return HotnessScore(total, per_day, last_activity)
