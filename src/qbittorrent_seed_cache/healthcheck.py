"""Container healthcheck: *liveness*, not data integrity.

Exits 1 (unhealthy) only for conditions a container restart can plausibly
fix or that make the daemon unable to work:

* the SSD cache dir is missing or not writable;
* the daemon's heartbeat (``.qbsc-heartbeat``, refreshed every tick and
  during long copies) is past its deadline — the daemon is hung.

A missing heartbeat is tolerated (daemon still starting, or an older daemon
that does not write one).

The anomaly marker (live symlinks into the SSD with a lost link->bulk
mapping) is a *data* problem that a restart cannot fix. It is printed — so it
shows up in ``docker inspect`` health logs — but does **not** fail the check:
with an ``autoheal``-style sidecar an unhealthy status means a kill/restart
every few minutes, which repaired nothing and killed every in-flight
promotion copy. Set ``QBSC_HEALTHCHECK_FAIL_ON_ANOMALY=1`` to restore the old
behaviour (e.g. if you alert on unhealthy status and do not auto-restart).

The qB endpoints are not checked here — a qB instance being temporarily down
is not a reason to fail the mover's healthcheck.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from .recovery import ANOMALY_MARKER, read_anomaly, read_heartbeat

FAIL_ON_ANOMALY_ENV = "QBSC_HEALTHCHECK_FAIL_ON_ANOMALY"
_ANOMALY_PREVIEW_LINES = 6


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    ssd = Path(os.environ.get("QBSC_SSD_DIR", "/var/lib/seed-cache"))
    if not ssd.is_dir():
        print(f"ssd_cache_dir not present: {ssd}", file=sys.stderr)
        return 1
    if not os.access(ssd, os.W_OK):
        print(f"ssd_cache_dir not writable: {ssd}", file=sys.stderr)
        return 1

    now = int(time.time())
    hb = read_heartbeat(ssd)
    if hb is not None and now > hb.stale_after_ts:
        print(
            f"daemon heartbeat stale: last beat {now - hb.ts}s ago, "
            f"deadline passed {now - hb.stale_after_ts}s ago",
            file=sys.stderr,
        )
        return 1

    status = 0
    anomaly = read_anomaly(ssd)
    if anomaly is not None:
        preview = "\n".join(anomaly.strip().splitlines()[:_ANOMALY_PREVIEW_LINES])
        fail = _env_true(FAIL_ON_ANOMALY_ENV)
        print(
            f"DATA ANOMALY ({'failing' if fail else 'not failing'} the healthcheck; "
            f"a restart does not fix it): {ssd / ANOMALY_MARKER}\n{preview}",
            file=sys.stderr if fail else sys.stdout,
        )
        if fail:
            status = 1
    if status == 0:
        age = f"{now - hb.ts}s ago" if hb is not None else "none yet"
        print(f"ok (heartbeat {age})")
    return status


if __name__ == "__main__":
    sys.exit(main())
