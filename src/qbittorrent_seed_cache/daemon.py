"""Main loop. Polls qB, scores torrents, applies promote/demote within quota.

The loop body is a single tick; the main loop awaits poll_interval_sec
between ticks. SIGTERM/SIGINT cancel the loop cleanly.
"""

from __future__ import annotations

import asyncio
import signal
import time

import structlog

from .config import Config
from .qbit_client import QbitClient
from .state import StateStore

log = structlog.get_logger(__name__)


async def _tick(config: Config, store: StateStore) -> None:
    """One iteration: snapshot + decide + apply."""
    now = int(time.time())

    async def poll_one(inst: object) -> None:
        # Imported here for type-friendliness in the stub.
        instance = inst  # type: ignore[assignment]
        async with QbitClient(
            name=instance.name,
            url=instance.url,
            username=instance.username,
            password=instance.password.get_secret_value(),
        ) as client:
            torrents = await client.torrents()
            for t in torrents:
                await asyncio.to_thread(
                    store.record,
                    instance=instance.name,
                    infohash=t.hash,
                    ts=now,
                    uploaded_session=t.uploaded_session,
                    upspeed=t.upspeed,
                )
            log.info("tick.snapshot", instance=instance.name, count=len(torrents))

    await asyncio.gather(*(poll_one(i) for i in config.instances))

    # Prune snapshots outside the rolling window.
    cutoff = now - config.hotness.window_days * 86_400
    pruned = await asyncio.to_thread(store.prune, before_ts=cutoff)
    if pruned:
        log.info("tick.pruned", rows=pruned)

    # TODO: hotness scoring, candidate selection, quota-bounded promote/demote.
    # Stubbed for the scaffolding commit — promotion/demotion logic lands next.


async def run_daemon(config: Config) -> None:
    log.info(
        "daemon.start",
        instances=[i.name for i in config.instances],
        poll_interval_sec=config.poll_interval_sec,
        dry_run=config.dry_run,
    )

    store = StateStore(config.state_db)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            try:
                await _tick(config, store)
            except Exception:
                log.exception("tick.failed")

            try:
                await asyncio.wait_for(stop.wait(), timeout=config.poll_interval_sec)
            except asyncio.TimeoutError:
                pass
    finally:
        store.close()
        log.info("daemon.stop")
