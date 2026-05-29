"""Container healthcheck.

Exits 0 if the SSD cache dir is mounted and writable and reconciliation
found no unrepairable state; 1 otherwise. The qB endpoints are not checked
here — a qB instance being temporarily down is not a reason to fail the
mover's healthcheck.

The anomaly marker (dropped by :mod:`qbittorrent_seed_cache.reconcile` when
it finds SSD/DB state it cannot reconcile automatically) turns the container
unhealthy so a data-integrity problem becomes visible instead of silent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .recovery import ANOMALY_MARKER


def main() -> int:
    ssd = Path(os.environ.get("QBSC_SSD_DIR", "/var/lib/seed-cache"))
    if not ssd.is_dir():
        print(f"ssd_cache_dir not present: {ssd}", file=sys.stderr)
        return 1
    if not os.access(ssd, os.W_OK):
        print(f"ssd_cache_dir not writable: {ssd}", file=sys.stderr)
        return 1
    marker = ssd / ANOMALY_MARKER
    if marker.exists():
        try:
            detail = marker.read_text(encoding="utf-8").strip()
        except OSError:
            detail = ""
        print(f"reconciliation anomaly present: {ssd / ANOMALY_MARKER}\n{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
