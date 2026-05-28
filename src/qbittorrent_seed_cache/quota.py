"""SSD quota accounting and candidate selection.

Two policies that work together:

- **Promote**: among cold torrents whose hotness exceeds promote_min, pick by
  upload_bytes_per_day DESC, subject to the soft quota.
- **Demote**: among hot torrents whose hotness fell below demote_max AND that
  have spent at least min_hot_minutes in the hot tier, demote in ascending
  hotness order.

The soft quota allows transient overshoots: a tick may push us past the
target if a promotion is already in flight. The next tick rebalances.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class QuotaState:
    used_bytes: int
    quota_bytes: int
    fs_free_bytes: int
    min_free_bytes: int

    @property
    def headroom_bytes(self) -> int:
        """Bytes we can still spend without violating quota or fs min-free."""
        return min(
            self.quota_bytes - self.used_bytes,
            self.fs_free_bytes - self.min_free_bytes,
        )

    @property
    def over_quota(self) -> bool:
        return self.used_bytes > self.quota_bytes


def current_state(
    *,
    ssd_cache_dir: Path,
    used_bytes: int,
    quota_gb: float,
    min_free_gb: float,
) -> QuotaState:
    usage = shutil.disk_usage(ssd_cache_dir)
    return QuotaState(
        used_bytes=used_bytes,
        quota_bytes=int(quota_gb * 1024**3),
        fs_free_bytes=usage.free,
        min_free_bytes=int(min_free_gb * 1024**3),
    )
