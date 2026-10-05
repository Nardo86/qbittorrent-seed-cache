"""Anomaly detection, rate-limited reporting and quarantine.

Production regression (2026-09/10): 37 torrents had symlinks dangling into
SSD dirs that no longer existed, with the link->bulk mapping lost. The daemon
logged one error per file per tick (~1.15M lines/month), the marker made the
healthcheck fail, and an autoheal sidecar restarted the container every ~3
minutes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from qbittorrent_seed_cache import recovery
from qbittorrent_seed_cache.anomaly import AnomalyReporter, find_unmapped_links
from qbittorrent_seed_cache.daemon import DaemonRuntime, _tick
from qbittorrent_seed_cache.qbit_client import TorrentInfo
from qbittorrent_seed_cache.state import StateStore

from .test_tick_integration import make_config, make_dirs, make_fake_client, make_torrent


def _ti(infohash: str, save_path: str, size: int, uploaded: int = 0) -> TorrentInfo:
    return TorrentInfo(
        hash=infohash, name=infohash, save_path=save_path, content_path=save_path,
        size=size, upspeed=0, uploaded_session=uploaded,
        last_activity=int(time.time()), state="uploading",
    )


def _dangling_torrent(
    bulk_root: Path, ssd: Path, infohash: str, names: list[str]
) -> tuple[TorrentInfo, list[dict[str, Any]], list[Path]]:
    """A torrent whose every link points into a *missing* SSD dir."""
    save = bulk_root / "storage" / "torrents" / f"rel-{infohash}"
    save.mkdir(parents=True)
    links = []
    for name in names:
        link = save / name
        os.symlink(ssd / infohash / name, link)  # target does not exist
        links.append(link)
    files = [{"name": n, "size": 100} for n in names]
    return _ti(infohash, f"/data/torrents/rel-{infohash}", 100 * len(names)), files, links


# --- find_unmapped_links -------------------------------------------------------


def test_find_unmapped_links_respects_db_and_sidecar(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    (ssd / "HOT").mkdir(parents=True)
    (ssd / "SIDE").mkdir(parents=True)
    links = {}
    for ih in ("HOT", "SIDE", "LOST"):
        link = tmp_path / f"link-{ih}"
        os.symlink(ssd / ih / "f.mkv", link)
        links[ih] = str(link)

    store = StateStore(tmp_path / "state.db")
    try:
        store.set_tier(infohash="HOT", tier="hot", since_ts=1, ssd_bytes=1,
                       bulk_targets={links["HOT"]: "/bulk/hot.mkv"})
        recovery.write_meta(ssd, infohash="SIDE", since_ts=1, ssd_bytes=1,
                            bulk_targets={links["SIDE"]: "/bulk/side.mkv"})
        found = find_unmapped_links(ssd, store, {ih: {link} for ih, link in links.items()})
        assert found == {"LOST": [links["LOST"]]}
    finally:
        store.close()


def test_find_unmapped_links_flags_unknown_link_of_hot_torrent(tmp_path: Path) -> None:
    """Per-link, not per-torrent: a hot torrent with one unmapped link is flagged
    (a demote would rm the SSD dir under that link)."""
    ssd = tmp_path / "ssd"
    (ssd / "HOT").mkdir(parents=True)
    known, unknown = tmp_path / "a", tmp_path / "b"
    os.symlink(ssd / "HOT" / "a", known)
    os.symlink(ssd / "HOT" / "b", unknown)
    store = StateStore(tmp_path / "state.db")
    try:
        store.set_tier(infohash="HOT", tier="hot", since_ts=1, ssd_bytes=1,
                       bulk_targets={str(known): "/bulk/a"})
        found = find_unmapped_links(ssd, store, {"HOT": {str(known), str(unknown)}})
        assert found == {"HOT": [str(unknown)]}
    finally:
        store.close()


def test_find_unmapped_links_rechecks_filesystem(tmp_path: Path) -> None:
    """A link retargeted away from the SSD since the poll is not reported."""
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    link = tmp_path / "link"
    os.symlink(tmp_path / "bulk.mkv", link)  # no longer into the SSD
    store = StateStore(tmp_path / "state.db")
    try:
        assert find_unmapped_links(ssd, store, {"IH": {str(link)}}) == {}
    finally:
        store.close()


# --- AnomalyReporter ---------------------------------------------------------------


def test_reporter_logs_on_change_and_then_rate_limits(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    rep = AnomalyReporter(log_interval_sec=3600)
    unmapped = {"IH1": ["/t/a", "/t/b"]}

    def errors(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [e for e in logs if e["log_level"] == "error"]

    with capture_logs() as logs:
        rep.report(ssd, unmapped, now_ts=1000, dry_run=False)
        rep.report(ssd, unmapped, now_ts=1060, dry_run=False)
        rep.report(ssd, unmapped, now_ts=1120, dry_run=False)
    assert len(errors(logs)) == 1
    assert errors(logs)[0]["torrents"] == 1 and errors(logs)[0]["links"] == 2

    text = recovery.read_anomaly(ssd)
    assert text is not None
    assert text.splitlines()[0].startswith("1 torrent(s), 2 symlink(s)")
    assert "IH1\t/t/a" in text
    assert "repair-dangling" in text

    with capture_logs() as logs:
        rep.report(ssd, unmapped, now_ts=1000 + 3600, dry_run=False)  # reminder
        rep.report(ssd, {"IH1": ["/t/a"]}, now_ts=1000 + 3660, dry_run=False)  # changed
    assert len(errors(logs)) == 2

    with capture_logs() as logs:
        rep.report(ssd, {}, now_ts=9999, dry_run=False)
    assert recovery.has_anomaly(ssd) is False
    assert [e["event"] for e in logs] == ["tick.anomaly_cleared"]


def test_reporter_keeps_first_seen_across_restarts(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    AnomalyReporter(log_interval_sec=60).report(ssd, {"IH": ["/l"]}, now_ts=0, dry_run=False)
    # A new process (restart) with a different set still reports the original date.
    AnomalyReporter(log_interval_sec=60).report(
        ssd, {"IH": ["/l", "/m"]}, now_ts=86_400, dry_run=False
    )
    text = recovery.read_anomaly(ssd)
    assert text is not None
    assert "first_seen: 1970-01-01T00:00:00Z" in text
    assert "updated: 1970-01-02T00:00:00Z" in text


def test_reporter_dry_run_does_not_write(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    AnomalyReporter(log_interval_sec=60).report(ssd, {"IH": ["/l"]}, now_ts=0, dry_run=True)
    assert recovery.has_anomaly(ssd) is False


# --- tick integration ---------------------------------------------------------------


async def test_dangling_links_reported_once_not_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk_root, ssd = make_dirs(tmp_path)
    ti, files, _ = _dangling_torrent(bulk_root, ssd, "LOST", ["e1.mkv", "e2.mkv", "e3.mkv"])
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient",
        make_fake_client({"qb1": {"torrents": [ti], "files": {"LOST": files}}}),
    )
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"])
    store = StateStore(config.state_db)
    rt = DaemonRuntime.for_config(config)
    try:
        with capture_logs() as logs:
            for _ in range(3):
                await _tick(config, store, rt)
        error_events = [e["event"] for e in logs if e["log_level"] == "error"]
        assert error_events == ["tick.anomaly_present"]
        assert recovery.has_anomaly(ssd)
        # Liveness heartbeat refreshed by the tick.
        hb = recovery.read_heartbeat(ssd)
        assert hb is not None and hb.stale_after_ts > time.time()
    finally:
        store.close()


async def test_quarantined_torrent_is_not_demoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hot torrent with one mapped and one unmapped link is cold by score, but
    demoting it would rm the SSD dir under the unmapped (still working) link.
    Quarantine leaves it alone; its SSD copy stays intact."""
    bulk_root, ssd = make_dirs(tmp_path)
    storage = bulk_root / "storage"
    save = storage / "torrents" / "rel-Q"
    save.mkdir(parents=True)
    for name in ("a.mkv", "b.mkv"):
        bulk = storage / "Films" / "Q" / name
        bulk.parent.mkdir(parents=True, exist_ok=True)
        bulk.write_bytes(b"x" * 100)
        (ssd / "Q").mkdir(exist_ok=True)
        (ssd / "Q" / name).write_bytes(b"x" * 100)
        os.symlink(ssd / "Q" / name, save / name)
    ti = _ti("Q", "/data/torrents/rel-Q", 200)
    files = [{"name": "a.mkv", "size": 100}, {"name": "b.mkv", "size": 100}]
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient",
        make_fake_client({"qb1": {"torrents": [ti], "files": {"Q": files}}}),
    )
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"])
    store = StateStore(config.state_db)
    try:
        # Only a.mkv's mapping is known; hot for a long time with zero upload.
        store.set_tier(infohash="Q", tier="hot", since_ts=1, ssd_bytes=200,
                       bulk_targets={str(save / "a.mkv"): str(storage / "Films/Q/a.mkv")})
        await _tick(config, store)

        tier = store.get_tier(infohash="Q")
        assert tier is not None and tier.tier == "hot"
        assert (ssd / "Q" / "b.mkv").is_file()
        assert (save / "b.mkv").read_bytes() == b"x" * 100
        text = recovery.read_anomaly(ssd)
        assert text is not None and str(save / "b.mkv") in text
    finally:
        store.close()


async def test_unrelated_torrent_still_promoted_during_anomaly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk_root, ssd = make_dirs(tmp_path)
    lost, lost_files, _ = _dangling_torrent(bulk_root, ssd, "LOST", ["x.mkv"])
    save_hot = bulk_root / "storage" / "torrents" / "rel-Hot"
    hot, hot_files = make_torrent(
        bulk_root=bulk_root, save_subdir_host=save_hot, save_path_qb="/data/torrents/rel-Hot",
        rel_files=[("video.mkv", b"x" * 200)], infohash="HOT",
        uploaded_session=400 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient",
        make_fake_client({"qb1": {"torrents": [lost, hot],
                                  "files": {"LOST": lost_files, "HOT": hot_files}}}),
    )
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"])
    store = StateStore(config.state_db)
    try:
        day_ago = int(time.time()) - 86_400
        store.record(instance="qb1", infohash="HOT", ts=day_ago, uploaded_session=0, upspeed=0)
        store.set_tier(infohash="HOT", tier="cold", since_ts=day_ago, ssd_bytes=0)
        await _tick(config, store)
        tier = store.get_tier(infohash="HOT")
        assert tier is not None and tier.tier == "hot"
        assert recovery.has_anomaly(ssd)
    finally:
        store.close()


async def test_suspend_promotions_on_anomaly_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk_root, ssd = make_dirs(tmp_path)
    lost, lost_files, _ = _dangling_torrent(bulk_root, ssd, "LOST", ["x.mkv"])
    save_hot = bulk_root / "storage" / "torrents" / "rel-Hot"
    hot, hot_files = make_torrent(
        bulk_root=bulk_root, save_subdir_host=save_hot, save_path_qb="/data/torrents/rel-Hot",
        rel_files=[("video.mkv", b"x" * 200)], infohash="HOT",
        uploaded_session=400 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient",
        make_fake_client({"qb1": {"torrents": [lost, hot],
                                  "files": {"LOST": lost_files, "HOT": hot_files}}}),
    )
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"])
    config = config.model_copy(update={"suspend_promotions_on_anomaly": True})
    store = StateStore(config.state_db)
    try:
        day_ago = int(time.time()) - 86_400
        store.record(instance="qb1", infohash="HOT", ts=day_ago, uploaded_session=0, upspeed=0)
        store.set_tier(infohash="HOT", tier="cold", since_ts=day_ago, ssd_bytes=0)
        await _tick(config, store)
        tier = store.get_tier(infohash="HOT")
        assert tier is not None and tier.tier == "cold"
        assert not (ssd / "HOT").exists()
    finally:
        store.close()


async def test_untracked_ssd_bytes_count_against_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSD content under an unmapped link is invisible to the DB; it must still
    consume quota so we don't over-promote on top of it (the disk-full incident)."""
    bulk_root, ssd = make_dirs(tmp_path)
    storage = bulk_root / "storage"
    save = storage / "torrents" / "rel-U"
    save.mkdir(parents=True)
    (ssd / "U").mkdir()
    (ssd / "U" / "big.mkv").write_bytes(b"x" * 1000)
    os.symlink(ssd / "U" / "big.mkv", save / "big.mkv")
    lost = _ti("U", "/data/torrents/rel-U", 1000)
    save_hot = storage / "torrents" / "rel-Hot"
    hot, hot_files = make_torrent(
        bulk_root=bulk_root, save_subdir_host=save_hot, save_path_qb="/data/torrents/rel-Hot",
        rel_files=[("video.mkv", b"x" * 200)], infohash="HOT",
        uploaded_session=400 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient",
        make_fake_client({"qb1": {"torrents": [lost, hot],
                                  "files": {"U": [{"name": "big.mkv", "size": 1000}],
                                            "HOT": hot_files}}}),
    )
    # Quota of 1100 bytes: 1000 untracked + 200 wanted does not fit.
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"],
                         quota_gb=1100 / 1024**3)
    store = StateStore(config.state_db)
    try:
        day_ago = int(time.time()) - 86_400
        store.record(instance="qb1", infohash="HOT", ts=day_ago, uploaded_session=0, upspeed=0)
        store.set_tier(infohash="HOT", tier="cold", since_ts=day_ago, ssd_bytes=0)
        await _tick(config, store)
        tier = store.get_tier(infohash="HOT")
        assert tier is not None and tier.tier == "cold"
        assert (ssd / "U" / "big.mkv").is_file()  # referenced → never reclaimed
    finally:
        store.close()
