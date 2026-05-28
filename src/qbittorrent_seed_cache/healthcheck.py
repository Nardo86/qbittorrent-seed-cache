"""Container healthcheck.

Exits 0 if the SSD cache dir is mounted and writable, 1 otherwise. The qB
endpoints are not checked here — a qB instance being temporarily down is
not a reason to fail the mover's healthcheck.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    ssd = Path(os.environ.get("QBSC_SSD_DIR", "/var/lib/seed-cache"))
    if not ssd.is_dir():
        print(f"ssd_cache_dir not present: {ssd}", file=sys.stderr)
        return 1
    if not os.access(ssd, os.W_OK):
        print(f"ssd_cache_dir not writable: {ssd}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
