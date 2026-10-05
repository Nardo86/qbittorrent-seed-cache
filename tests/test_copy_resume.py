"""Promotion copies survive a kill/stop mid-way and resume instead of restarting.

Production regression: the daemon was restarted every ~3 minutes while
copying a 20 GB file. The random-named tmp fragment was left behind by
SIGKILL, reclaimed as an orphan dir by the next start and the copy began
again from byte 0 — 29k `promote.copy` lines, one `promote.ok` in a month.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from qbittorrent_seed_cache import daemon, recovery, symlinks
from qbittorrent_seed_cache.daemon import _cleanup_fs_orphans, _tick
from qbittorrent_seed_cache.mover import promote
from qbittorrent_seed_cache.resolver import aggregate, resolve
from qbittorrent_seed_cache.state import StateStore
from qbittorrent_seed_cache.symlinks import CopyInterrupted, partial_paths, safe_copy

from .test_tick_integration import make_config, make_dirs, make_fake_client, make_torrent

PAYLOAD = bytes(range(256)) * 64  # 16 KiB


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(symlinks, "COPY_CHUNK_BYTES", 1024)
    monkeypatch.setattr(symlinks, "current_boot_id", lambda: "boot-1")


def _stop_after(n_bytes: int) -> tuple[threading.Event, Any]:
    ev = threading.Event()

    def on_progress(done: int, _total: int) -> None:
        if done >= n_bytes:
            ev.set()

    return ev, on_progress


def _interrupted_copy(src: Path, dest: Path, at: int) -> None:
    ev, cb = _stop_after(at)
    with pytest.raises(CopyInterrupted):
        safe_copy(src, dest, stop=ev, on_progress=cb)


def test_interrupted_copy_keeps_partial_and_resumes(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(PAYLOAD)
    dest = tmp_path / "ssd" / "dest.mkv"

    _interrupted_copy(src, dest, 4096)
    part, meta = partial_paths(dest)
    assert not dest.exists()
    assert part.stat().st_size == 4096
    assert meta.is_file()

    progress: list[int] = []
    resumed_from = safe_copy(src, dest, on_progress=lambda d, _t: progress.append(d))
    assert resumed_from == 4096
    assert progress[0] == 4096 + 1024  # did not re-read the first 4 KiB
    assert dest.read_bytes() == PAYLOAD
    assert not part.exists() and not meta.exists()


def test_partial_from_another_boot_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(PAYLOAD)
    dest = tmp_path / "dest.mkv"
    _interrupted_copy(src, dest, 4096)
    monkeypatch.setattr(symlinks, "current_boot_id", lambda: "boot-2")  # host rebooted
    assert safe_copy(src, dest) == 0
    assert dest.read_bytes() == PAYLOAD


def test_partial_of_a_changed_source_is_discarded(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_bytes(PAYLOAD)
    dest = tmp_path / "dest.mkv"
    _interrupted_copy(src, dest, 4096)
    new = PAYLOAD[::-1]
    src.write_bytes(new)
    os.utime(src, ns=(1, 1))  # different mtime
    assert safe_copy(src, dest) == 0
    assert dest.read_bytes() == new


def test_without_boot_id_never_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(symlinks, "current_boot_id", lambda: None)
    src = tmp_path / "src.mkv"
    src.write_bytes(PAYLOAD)
    dest = tmp_path / "dest.mkv"
    _interrupted_copy(src, dest, 4096)
    assert safe_copy(src, dest) == 0
    assert dest.read_bytes() == PAYLOAD


def test_failed_copy_discards_partial(tmp_path: Path) -> None:
    dest = tmp_path / "dest.mkv"
    with pytest.raises(FileNotFoundError):
        safe_copy(tmp_path / "missing.mkv", dest)
    assert not any(p.name.startswith(".dest.mkv") for p in tmp_path.iterdir())


# --- daemon integration -------------------------------------------------------------


def _hot_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    bulk_root, ssd = make_dirs(tmp_path)
    save = bulk_root / "storage" / "torrents" / "rel-Big"
    ti, files = make_torrent(
        bulk_root=bulk_root, save_subdir_host=save, save_path_qb="/data/torrents/rel-Big",
        rel_files=[("big.mkv", PAYLOAD)], infohash="BIG",
        uploaded_session=400 * 1024 * 1024,
    )
    data = {"qb1": {"torrents": [ti], "files": {"BIG": files}}}
    monkeypatch.setattr("qbittorrent_seed_cache.daemon.QbitClient", make_fake_client(data))
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"])
    store = StateStore(config.state_db)
    day_ago = int(time.time()) - 86_400
    store.record(instance="qb1", infohash="BIG", ts=day_ago, uploaded_session=0, upspeed=0)
    store.set_tier(infohash="BIG", tier="cold", since_ts=day_ago, ssd_bytes=0)
    r = resolve(instance="qb1", torrent=ti, files=files, ssd_cache_dir=ssd,
                path_map={"/data": str(bulk_root / "storage")},
                managed_paths=[bulk_root / "storage"])
    assert r is not None
    lt = aggregate([r])["BIG"]
    return config, store, ssd, save, lt


async def test_restart_mid_promotion_resumes_instead_of_reclaiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, ssd, save, lt = _hot_candidate(tmp_path, monkeypatch)
    try:
        # First daemon life: stopped (SIGTERM) half-way through the copy.
        ev, cb = _stop_after(8192)
        with pytest.raises(CopyInterrupted):
            promote(lt.layouts, now_ts=int(time.time()), stop=ev, on_progress=cb)
        assert recovery.read_promotion_intent_ts(ssd, "BIG") is not None
        link = save / "big.mkv"
        assert not Path(os.readlink(link)).is_absolute()  # untouched, still bulk

        # Second life: the dir is kept and the copy resumes.
        with capture_logs() as logs:
            await _tick(config, store)
        events = [e["event"] for e in logs]
        assert "orphan.keep_pending_promotion" in events
        assert "orphan.fs_reclaim" not in events
        resumed = [e for e in logs if e["event"] == "promote.copy_resumed"]
        assert resumed and resumed[0]["resumed_from"] == 8192

        tier = store.get_tier(infohash="BIG")
        assert tier is not None and tier.tier == "hot"
        assert Path(os.readlink(link)) == ssd / "BIG" / "big.mkv"
        assert link.read_bytes() == PAYLOAD
        assert recovery.read_promotion_intent_ts(ssd, "BIG") is None
        assert recovery.read_meta(ssd, "BIG") is not None
    finally:
        store.close()


def test_pending_promotion_reclaimed_when_torrent_gone_or_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, ssd, _save, lt = _hot_candidate(tmp_path, monkeypatch)
    try:
        ev, cb = _stop_after(4096)
        started = int(time.time())
        with pytest.raises(CopyInterrupted):
            promote(lt.layouts, now_ts=started, stop=ev, on_progress=cb)

        # Still live and fresh → kept.
        assert _cleanup_fs_orphans(config, store, set(), {"BIG"}, started + 60) == 0
        assert (ssd / "BIG").is_dir()
        # Too old → reclaimed even if live.
        assert _cleanup_fs_orphans(
            config, store, set(), {"BIG"}, started + daemon.PROMOTION_RESUME_TTL_SEC + 1
        ) == 1
        assert not (ssd / "BIG").exists()
    finally:
        store.close()


async def test_halt_interrupts_promotion_in_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, ssd, save, _lt = _hot_candidate(tmp_path, monkeypatch)
    rt = daemon.DaemonRuntime.for_config(config)
    real_progress = daemon._copy_progress

    def halting_progress(cfg: Any, ih: str) -> Any:
        inner = real_progress(cfg, ih)

        def cb(done: int, total: int) -> None:
            inner(done, total)
            if done >= 2048:
                rt.halt.set()  # what the SIGTERM handler does

        return cb

    monkeypatch.setattr(daemon, "_copy_progress", halting_progress)
    try:
        with capture_logs() as logs:
            await _tick(config, store, rt)
        assert "promote.interrupted" in [e["event"] for e in logs]
        tier = store.get_tier(infohash="BIG")
        assert tier is not None and tier.tier == "cold"
        part, _ = partial_paths(ssd / "BIG" / "big.mkv")
        assert part.stat().st_size == 2048
        assert not Path(os.readlink(save / "big.mkv")).is_absolute()
    finally:
        store.close()
