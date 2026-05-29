"""Tests for startup reconciliation between the DB and the SSD filesystem.

Covers the three drift scenarios reconcile() handles, the forward-migration
of pre-sidecar promotions, and the incident regression: a wiped DB must be
rebuilt from the on-disk sidecars so the daemon does not over-promote on top
of an SSD it forgot about.
"""

from __future__ import annotations

import os
from pathlib import Path

from qbittorrent_seed_cache import recovery
from qbittorrent_seed_cache.config import Config
from qbittorrent_seed_cache.reconcile import reconcile_startup
from qbittorrent_seed_cache.state import StateStore

# --- fixture builders ------------------------------------------------------


def make_config(tmp_path: Path, ssd: Path, *, dry_run: bool = False, quota_gb: float = 100) -> Config:
    return Config(
        ssd_cache_dir=ssd,
        quota_gb=quota_gb,
        min_free_gb=0,
        bulk_root=tmp_path / "media",
        managed_paths=[tmp_path / "media" / "storage"],
        instances=[],
        state_db=tmp_path / "state.db",
        log_format="console",
        log_level="WARNING",
        dry_run=dry_run,
    )


def make_promoted_on_disk(
    tmp_path: Path,
    ssd: Path,
    infohash: str,
    *,
    rel: str = "movie.mkv",
    content: bytes = b"x" * 100,
    write_sidecar: bool = True,
) -> tuple[Path, Path]:
    """Build the on-disk state of a promoted torrent.

    Creates the bulk file, the SSD copy, an absolute symlink pointing into
    the SSD (as promote() leaves it), and optionally the sidecar. Returns
    (link, bulk_file).
    """
    storage = tmp_path / "media" / "storage"
    bulk_file = storage / "Films" / infohash / rel
    bulk_file.parent.mkdir(parents=True, exist_ok=True)
    bulk_file.write_bytes(content)

    ssd_target = ssd / infohash / rel
    ssd_target.parent.mkdir(parents=True, exist_ok=True)
    ssd_target.write_bytes(content)

    save_dir = storage / "torrents" / f"rel-{infohash}"
    save_dir.mkdir(parents=True, exist_ok=True)
    link = save_dir / rel
    os.symlink(ssd_target, link)  # absolute, into SSD

    if write_sidecar:
        recovery.write_meta(
            ssd,
            infohash=infohash,
            since_ts=1700,
            ssd_bytes=len(content),
            bulk_targets={str(link): str(bulk_file)},
        )
    return link, bulk_file


# --- case A: DB fresh / lost, SSD intact -----------------------------------


def test_reconcile_rebuilds_tier_from_sidecar(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    link, bulk = make_promoted_on_disk(tmp_path, ssd, "HASHA", content=b"x" * 500)

    config = make_config(tmp_path, ssd)
    store = StateStore(config.state_db)  # fresh, empty DB
    try:
        report = reconcile_startup(config, store)

        assert report.rebuilt == 1
        tier = store.get_tier(infohash="HASHA")
        assert tier is not None and tier.tier == "hot"
        assert tier.bulk_targets == {str(link): str(bulk)}
        # The crux of the incident fix: the daemon now knows the SSD is in use.
        assert store.hot_total_bytes() == 500
        assert recovery.has_anomaly(ssd) is False
    finally:
        store.close()


def test_incident_regression_wiped_db_does_not_lose_accounting(tmp_path: Path) -> None:
    """Replaying the incident: a torrent is promoted (sidecar on disk), then
    the DB is replaced by a fresh one. Reconciliation must restore the hot
    accounting so headroom is computed correctly and the daemon does not
    over-promote on top of the existing 'invisible' cache."""
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    make_promoted_on_disk(tmp_path, ssd, "HASHBIG", content=b"x" * 9999)

    config = make_config(tmp_path, ssd, quota_gb=100)
    fresh_db = StateStore(config.state_db)
    try:
        # Before reconcile a naive fresh DB sees 0 bytes used -> full quota free.
        assert fresh_db.hot_total_bytes() == 0
        reconcile_startup(config, fresh_db)
        # After reconcile the cache is accounted for.
        assert fresh_db.hot_total_bytes() == 9999
    finally:
        fresh_db.close()


# --- case B: SSD dir deleted, DB intact ------------------------------------


def test_reconcile_demotes_when_ssd_dir_missing(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()

    storage = tmp_path / "media" / "storage"
    bulk_file = storage / "Films" / "HASHB" / "movie.mkv"
    bulk_file.parent.mkdir(parents=True, exist_ok=True)
    bulk_file.write_bytes(b"y" * 200)

    # Symlink dangles into a non-existent SSD path.
    save_dir = storage / "torrents" / "rel-HASHB"
    save_dir.mkdir(parents=True, exist_ok=True)
    link = save_dir / "movie.mkv"
    ssd_target = ssd / "HASHB" / "movie.mkv"
    os.symlink(ssd_target, link)
    assert not ssd_target.exists()  # SSD dir genuinely gone

    config = make_config(tmp_path, ssd)
    store = StateStore(config.state_db)
    try:
        store.set_tier(
            infohash="HASHB", tier="hot", since_ts=1, ssd_bytes=200,
            bulk_targets={str(link): str(bulk_file)},
        )

        report = reconcile_startup(config, store)

        assert report.redemoted == 1
        # Tier dropped, symlink repaired to the bulk file (relative).
        assert store.get_tier(infohash="HASHB") is None
        assert link.is_symlink()
        assert not Path(os.readlink(link)).is_absolute()
        assert link.resolve() == bulk_file.resolve()
        assert recovery.has_anomaly(ssd) is False
    finally:
        store.close()


# --- unmapped SSD dir: deferred to the tick, no anomaly at reconcile -------


def test_reconcile_defers_unmapped_ssd_dir(tmp_path: Path) -> None:
    """An SSD dir with content but no sidecar and no DB row can't be judged at
    startup (orphan vs data-loss needs live qB data). Reconcile defers it and
    must NOT touch the anomaly marker — that's the tick's job."""
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    make_promoted_on_disk(tmp_path, ssd, "HASHC", write_sidecar=False)

    config = make_config(tmp_path, ssd)
    store = StateStore(config.state_db)
    try:
        report = reconcile_startup(config, store)
        assert report.deferred == 1
        # Reconcile never sets the marker.
        assert recovery.has_anomaly(ssd) is False
        # The dir is left intact for the tick to reclaim or flag.
        assert (ssd / "HASHC").is_dir()
    finally:
        store.close()


def test_reconcile_ignores_empty_dir(tmp_path: Path) -> None:
    """An empty (payload-less) SSD subdir — e.g. a stray artifact like the
    legacy `files/` dir — must not be deferred or flagged."""
    ssd = tmp_path / "ssd"
    (ssd / "files").mkdir(parents=True)  # empty, no sidecar, not in DB

    config = make_config(tmp_path, ssd)
    store = StateStore(config.state_db)
    try:
        report = reconcile_startup(config, store)
        assert report.deferred == 0
        assert recovery.has_anomaly(ssd) is False
    finally:
        store.close()


def test_reconcile_drops_unrepairable_hot_row(tmp_path: Path) -> None:
    """A hot row with no bulk_targets and no SSD dir is stale garbage: drop it
    (nothing to retarget, nothing to clean). No marker from reconcile."""
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    config = make_config(tmp_path, ssd)
    store = StateStore(config.state_db)
    try:
        store.set_tier(infohash="HASHD", tier="hot", since_ts=1, ssd_bytes=10)
        report = reconcile_startup(config, store)
        assert report.dropped == 1
        assert store.get_tier(infohash="HASHD") is None
        assert recovery.has_anomaly(ssd) is False
    finally:
        store.close()


# --- forward migration of pre-sidecar promotions ---------------------------


def test_reconcile_forward_migrates_legacy_promotion(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    # On disk: promoted dir WITHOUT a sidecar (legacy).
    link, bulk = make_promoted_on_disk(tmp_path, ssd, "HASHLEG", content=b"z" * 321, write_sidecar=False)

    config = make_config(tmp_path, ssd)
    store = StateStore(config.state_db)
    try:
        # DB still knows about it (the pre-sidecar world).
        store.set_tier(
            infohash="HASHLEG", tier="hot", since_ts=42, ssd_bytes=321,
            bulk_targets={str(link): str(bulk)},
        )

        report = reconcile_startup(config, store)

        assert report.sidecars_written == 1
        # A sidecar now exists so a future DB loss is covered.
        meta = recovery.read_meta(ssd, "HASHLEG")
        assert meta is not None
        assert meta.bulk_targets == {str(link): str(bulk)}
        assert meta.ssd_bytes == 321
        # Tier left intact.
        assert store.get_tier(infohash="HASHLEG") is not None
    finally:
        store.close()


# --- dry-run is read-only --------------------------------------------------


def test_reconcile_dry_run_makes_no_changes(tmp_path: Path) -> None:
    """Dry-run reports what it would do (rebuild from sidecar) but mutates
    neither the DB nor the filesystem."""
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    make_promoted_on_disk(tmp_path, ssd, "HASHDRY", content=b"q" * 256)  # sidecar written

    config = make_config(tmp_path, ssd, dry_run=True)
    store = StateStore(config.state_db)  # fresh DB
    try:
        report = reconcile_startup(config, store)
        # Would rebuild, but the DB stays empty in dry-run.
        assert report.rebuilt == 1
        assert store.get_tier(infohash="HASHDRY") is None
        assert recovery.has_anomaly(ssd) is False
    finally:
        store.close()
