"""Integration tests for daemon._tick.

Mocks the qB Web API via a FakeQbClient monkeypatched into the daemon
module, and builds a real on-disk symlink tree under tmp_path. Exercises
the full tick: snapshot → aggregate → score → orphan cleanup → demote →
promote, including multi-instance dedup.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from qbittorrent_seed_cache.config import Config, HotnessConfig, InstanceConfig
from qbittorrent_seed_cache.daemon import _tick
from qbittorrent_seed_cache.qbit_client import TorrentInfo
from qbittorrent_seed_cache.state import StateStore

# --- fake qB client --------------------------------------------------------


def make_fake_client(data: dict[str, dict[str, Any]]) -> type:
    """Return a class that, when used as `QbitClient(name=...)`, serves
    pre-canned torrents/files from `data[name]`.

    data = {
      "qb-name": {
         "torrents": [TorrentInfo, ...],
         "files":    {infohash: [{"name": rel, "size": int}, ...]},
      }, ...
    }
    """

    class _Fake:
        def __init__(
            self, *, name: str, url: str, username: str, password: str
        ) -> None:
            self.name = name
            self._d = data[name]

        async def __aenter__(self) -> _Fake:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def torrents(self) -> list[TorrentInfo]:
            return list(self._d["torrents"])

        async def torrent_files(self, infohash: str) -> list[dict[str, Any]]:
            return list(self._d["files"][infohash])

    return _Fake


# --- on-disk fixture builders ---------------------------------------------


def make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (bulk_root, ssd_cache_dir). bulk_root contains a 'storage' subtree."""
    bulk_root = tmp_path / "media"
    storage = bulk_root / "storage"
    storage.mkdir(parents=True)
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    return bulk_root, ssd


def make_torrent(
    *,
    bulk_root: Path,            # host root, e.g. /tmp/.../media
    save_subdir_host: Path,     # absolute path in host fs where the torrent's save dir is
    save_path_qb: str,          # what qB reports (container path)
    rel_files: list[tuple[str, bytes]],
    infohash: str,
    uploaded_session: int = 0,
    upspeed: int = 0,
) -> tuple[TorrentInfo, list[dict[str, Any]]]:
    """Create real bulk files and host-side symlinks, return (TorrentInfo, files)."""
    files_api: list[dict[str, Any]] = []
    total_size = 0
    save_subdir_host.mkdir(parents=True, exist_ok=True)

    for rel, content in rel_files:
        # Real file under Films/... (bulk).
        bulk_target = bulk_root / "storage" / "Films" / infohash / rel
        bulk_target.parent.mkdir(parents=True, exist_ok=True)
        bulk_target.write_bytes(content)

        # Symlink in the save_subdir (what qB sees as the torrent's content).
        link = save_subdir_host / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        rel_target = os.path.relpath(bulk_target, link.parent)
        os.symlink(rel_target, link)

        files_api.append({"name": rel, "size": len(content)})
        total_size += len(content)

    ti = TorrentInfo(
        hash=infohash,
        name=save_subdir_host.name,
        save_path=save_path_qb,
        content_path=save_path_qb,
        size=total_size,
        upspeed=upspeed,
        uploaded_session=uploaded_session,
        last_activity=int(time.time()),
        state="uploading",
    )
    return ti, files_api


def make_config(
    tmp_path: Path,
    *,
    bulk_root: Path,
    ssd: Path,
    instance_names: list[str],
    quota_gb: float = 100,
    promote_min_mb: float = 50,
    demote_max_mb: float = 5,
) -> Config:
    instances = [
        InstanceConfig(
            name=name,
            url=f"http://{name}.invalid",
            username="x",
            password="x",
            path_map={"/data": str(bulk_root / "storage")},
        )
        for name in instance_names
    ]
    return Config(
        ssd_cache_dir=ssd,
        quota_gb=quota_gb,
        min_free_gb=0,
        bulk_root=bulk_root,
        managed_paths=[bulk_root / "storage"],
        instances=instances,
        hotness=HotnessConfig(
            window_days=14,
            promote_min_upload_mb=promote_min_mb,
            demote_max_upload_mb=demote_max_mb,
            min_hot_minutes=0,    # zero so age gating doesn't block test transitions
            min_cold_minutes=0,
        ),
        poll_interval_sec=30,
        state_db=tmp_path / "state.db",
        log_format="console",
        log_level="WARNING",
        dry_run=False,
        max_concurrent_promotions=4,
    )


# --- tests -----------------------------------------------------------------


async def test_baseline_tick_bootstraps_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First tick against an empty state DB: bootstraps every torrent as cold,
    no demotions, no promotions (no history yet)."""
    bulk_root, ssd = make_dirs(tmp_path)

    save_qb = bulk_root / "storage" / "torrents" / "rel-A"
    ti, files = make_torrent(
        bulk_root=bulk_root,
        save_subdir_host=save_qb,
        save_path_qb="/data/torrents/rel-A",
        rel_files=[("movie.mkv", b"video" * 50)],
        infohash="HASHA",
    )

    data = {"qb1": {"torrents": [ti], "files": {"HASHA": files}}}
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient", make_fake_client(data)
    )

    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"])
    store = StateStore(config.state_db)
    try:
        await _tick(config, store)

        # Torrent bootstrapped as cold; SSD untouched; symlink still relative.
        tier = store.get_tier(infohash="HASHA")
        assert tier is not None
        assert tier.tier == "cold"
        assert tier.since_ts == pytest.approx(int(time.time()), abs=5)
        assert tier.bulk_targets is None
        assert not (ssd / "HASHA").exists()
        link = save_qb / "movie.mkv"
        assert link.is_symlink()
        # Symlink should still be relative (cold state).
        assert not Path(os.readlink(link)).is_absolute()
    finally:
        store.close()


async def test_tick_promotes_hot_torrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With enough rolling-window history showing high upload, the torrent
    is promoted to SSD on the next tick."""
    bulk_root, ssd = make_dirs(tmp_path)

    save_qb = bulk_root / "storage" / "torrents" / "rel-Hot"
    # 200 MB synthetic content (small but the size is just an int, not real bytes).
    ti, files = make_torrent(
        bulk_root=bulk_root,
        save_subdir_host=save_qb,
        save_path_qb="/data/torrents/rel-Hot",
        rel_files=[("video.mkv", b"x" * 200)],
        infohash="HASHHOT",
        uploaded_session=400 * 1024 * 1024,  # current snapshot will be 400 MB
    )

    data = {"qb1": {"torrents": [ti], "files": {"HASHHOT": files}}}
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient", make_fake_client(data)
    )
    config = make_config(
        tmp_path,
        bulk_root=bulk_root,
        ssd=ssd,
        instance_names=["qb1"],
        promote_min_mb=50,
    )

    store = StateStore(config.state_db)
    try:
        # Seed history: a snapshot 86_400 seconds ago at 0 MB. The current
        # tick's snapshot of 400 MB / 1 day = 400 MB/day → well above 50 MB/day.
        day_ago = int(time.time()) - 86_400
        store.record(
            instance="qb1", infohash="HASHHOT", ts=day_ago,
            uploaded_session=0, upspeed=0,
        )
        # Pre-bootstrap tier as cold so the promotion path doesn't get short-
        # circuited by the bootstrap step (bootstrap only runs for unknown tiers).
        store.set_tier(infohash="HASHHOT", tier="cold", since_ts=day_ago, ssd_bytes=0)

        await _tick(config, store)

        # Tier flipped to hot; SSD copy made; symlink absolute → SSD.
        tier = store.get_tier(infohash="HASHHOT")
        assert tier is not None
        assert tier.tier == "hot"
        # bulk_targets persisted so the next tick can resolve the symlink
        # that now points into the SSD.
        assert tier.bulk_targets is not None
        assert len(tier.bulk_targets) == 1

        ssd_file = ssd / "HASHHOT" / "video.mkv"
        assert ssd_file.is_file()

        link = save_qb / "video.mkv"
        assert link.is_symlink()
        link_target = Path(os.readlink(link))
        assert link_target == ssd_file
    finally:
        store.close()


async def test_tick_cleans_up_orphan_hot_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hot tier row whose infohash no longer exists in any qB is cleaned up:
    SSD content removed, tier row dropped."""
    bulk_root, ssd = make_dirs(tmp_path)

    # Empty qB.
    data: dict[str, dict[str, Any]] = {"qb1": {"torrents": [], "files": {}}}
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient", make_fake_client(data)
    )
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"])

    store = StateStore(config.state_db)
    try:
        # Plant orphan: tier=hot for an infohash + SSD content.
        orphan_dir = ssd / "ORPHAN"
        orphan_dir.mkdir()
        (orphan_dir / "stale.mkv").write_bytes(b"garbage")
        store.set_tier(infohash="ORPHAN", tier="hot", since_ts=1, ssd_bytes=999)

        await _tick(config, store)

        assert store.get_tier(infohash="ORPHAN") is None
        assert not orphan_dir.exists()
    finally:
        store.close()


async def test_tick_multi_instance_dedup_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same infohash on two qB instances is promoted once on SSD and
    counts once toward the quota. Both instances' symlinks are retargeted."""
    bulk_root, ssd = make_dirs(tmp_path)

    save_a = bulk_root / "storage" / "torrents" / "rel-A"
    save_b = bulk_root / "storage" / "torrents" / "rel-B"

    # Same infohash, different save paths → different symlinks.
    ti_a, files_a = make_torrent(
        bulk_root=bulk_root, save_subdir_host=save_a,
        save_path_qb="/data/torrents/rel-A",
        rel_files=[("media.mkv", b"x" * 300)],
        infohash="HASHSHARED", uploaded_session=200 * 1024 * 1024,
    )
    # Create the second instance's symlink to the SAME bulk file already
    # created by the first call. make_torrent's second invocation would
    # re-write the bulk file; instead we just build the symlink manually.
    save_b.mkdir(parents=True)
    bulk_target = bulk_root / "storage" / "Films" / "HASHSHARED" / "media.mkv"
    link_b = save_b / "media.mkv"
    os.symlink(os.path.relpath(bulk_target, save_b), link_b)
    ti_b = TorrentInfo(
        hash="HASHSHARED",
        name="rel-B",
        save_path="/data/torrents/rel-B",
        content_path="/data/torrents/rel-B",
        size=ti_a.size,
        upspeed=0,
        uploaded_session=100 * 1024 * 1024,
        last_activity=int(time.time()),
        state="uploading",
    )
    files_b = [{"name": "media.mkv", "size": ti_a.size}]

    data = {
        "qb1": {"torrents": [ti_a], "files": {"HASHSHARED": files_a}},
        "qb2": {"torrents": [ti_b], "files": {"HASHSHARED": files_b}},
    }
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient", make_fake_client(data)
    )
    config = make_config(
        tmp_path, bulk_root=bulk_root, ssd=ssd,
        instance_names=["qb1", "qb2"], promote_min_mb=50,
    )

    store = StateStore(config.state_db)
    try:
        day_ago = int(time.time()) - 86_400
        # Sum across instances must exceed promote_min_mb. Each contributes
        # via its own (instance, infohash) history.
        store.record(instance="qb1", infohash="HASHSHARED", ts=day_ago,
                     uploaded_session=0, upspeed=0)
        store.record(instance="qb2", infohash="HASHSHARED", ts=day_ago,
                     uploaded_session=0, upspeed=0)
        store.set_tier(infohash="HASHSHARED", tier="cold",
                       since_ts=day_ago, ssd_bytes=0)

        await _tick(config, store)

        # Tier hot; quota counted once.
        tier = store.get_tier(infohash="HASHSHARED")
        assert tier is not None and tier.tier == "hot"
        # Both instances' links recorded in bulk_targets.
        assert tier.bulk_targets is not None
        assert len(tier.bulk_targets) == 2
        # ssd_bytes should equal one copy, not two.
        assert store.hot_total_bytes() == ti_a.size

        ssd_file = ssd / "HASHSHARED" / "media.mkv"
        assert ssd_file.is_file()

        # BOTH symlinks retargeted to the same SSD copy.
        for link in (save_a / "media.mkv", save_b / "media.mkv"):
            assert link.is_symlink()
            assert Path(os.readlink(link)) == ssd_file
    finally:
        store.close()


async def test_hot_torrent_survives_subsequent_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the orphan-cleanup bug: after a torrent is promoted, its
    symlink resolves into the SSD (not the bulk fs). The next tick's
    resolver MUST still recognise the torrent as live, otherwise the
    orphan-cleanup pass deletes its SSD content. We persist bulk_targets at
    promote time and feed them back to resolve()."""
    bulk_root, ssd = make_dirs(tmp_path)

    save_qb = bulk_root / "storage" / "torrents" / "rel-Hot"
    ti, files = make_torrent(
        bulk_root=bulk_root,
        save_subdir_host=save_qb,
        save_path_qb="/data/torrents/rel-Hot",
        rel_files=[("video.mkv", b"x" * 200)],
        infohash="HASHSURVIVE",
        uploaded_session=400 * 1024 * 1024,
    )

    data = {"qb1": {"torrents": [ti], "files": {"HASHSURVIVE": files}}}
    monkeypatch.setattr(
        "qbittorrent_seed_cache.daemon.QbitClient", make_fake_client(data)
    )
    config = make_config(
        tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1"],
        promote_min_mb=50,
    )

    store = StateStore(config.state_db)
    try:
        day_ago = int(time.time()) - 86_400
        store.record(instance="qb1", infohash="HASHSURVIVE", ts=day_ago,
                     uploaded_session=0, upspeed=0)
        store.set_tier(infohash="HASHSURVIVE", tier="cold",
                       since_ts=day_ago, ssd_bytes=0)

        # Tick 1: should promote.
        await _tick(config, store)

        ssd_file = ssd / "HASHSURVIVE" / "video.mkv"
        assert ssd_file.is_file(), "SSD copy missing after tick 1"
        tier_after_1 = store.get_tier(infohash="HASHSURVIVE")
        assert tier_after_1 is not None and tier_after_1.tier == "hot"

        link = save_qb / "video.mkv"
        assert Path(os.readlink(link)) == ssd_file

        # Tick 2: with the same qB input, the torrent is still live. The
        # symlink resolves into the SSD now, so the resolver needs
        # bulk_targets from the tier to keep classifying it as live.
        await _tick(config, store)

        # SSD content + tier survived.
        assert ssd_file.is_file(), "SSD copy was orphan-cleaned!"
        tier_after_2 = store.get_tier(infohash="HASHSURVIVE")
        assert tier_after_2 is not None and tier_after_2.tier == "hot"
        # Symlink still pointing at the SSD.
        assert Path(os.readlink(link)) == ssd_file
    finally:
        store.close()
