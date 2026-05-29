"""Unit tests for the recovery sidecar primitives and anomaly marker."""

from __future__ import annotations

import json
from pathlib import Path

from qbittorrent_seed_cache import healthcheck, recovery


def test_write_then_read_meta_roundtrip(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    (ssd / "HASH1").mkdir(parents=True)
    bulk_targets = {
        "/data/torrents/rel/a.mkv": "/mnt/media/storage/Films/A/a.mkv",
        "/data/torrents/rel/b.mkv": "/mnt/media/storage/Films/B/b.mkv",
    }
    recovery.write_meta(
        ssd, infohash="HASH1", since_ts=1700, ssd_bytes=4242, bulk_targets=bulk_targets
    )

    meta = recovery.read_meta(ssd, "HASH1")
    assert meta is not None
    assert meta.infohash == "HASH1"
    assert meta.since_ts == 1700
    assert meta.ssd_bytes == 4242
    assert meta.bulk_targets == bulk_targets

    # Sidecar lives inside the infohash dir.
    assert recovery.meta_path(ssd, "HASH1").is_file()


def test_read_meta_absent_returns_none(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    assert recovery.read_meta(ssd, "NOPE") is None


def test_read_meta_corrupt_json_returns_none(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    (ssd / "HASH").mkdir(parents=True)
    recovery.meta_path(ssd, "HASH").write_text("{ not valid json", encoding="utf-8")
    assert recovery.read_meta(ssd, "HASH") is None


def test_read_meta_bad_version_returns_none(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    (ssd / "HASH").mkdir(parents=True)
    recovery.meta_path(ssd, "HASH").write_text(
        json.dumps({"schema_version": 999, "bulk_targets": {"a": "b"},
                    "since_ts": 1, "ssd_bytes": 1}),
        encoding="utf-8",
    )
    assert recovery.read_meta(ssd, "HASH") is None


def test_read_meta_empty_bulk_targets_returns_none(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    (ssd / "HASH").mkdir(parents=True)
    recovery.meta_path(ssd, "HASH").write_text(
        json.dumps({"schema_version": recovery.META_SCHEMA_VERSION,
                    "bulk_targets": {}, "since_ts": 1, "ssd_bytes": 1}),
        encoding="utf-8",
    )
    assert recovery.read_meta(ssd, "HASH") is None


def test_iter_ssd_infohash_dirs_skips_dotfiles_and_files(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    (ssd / "HASHA").mkdir()
    (ssd / "HASHB").mkdir()
    (ssd / ".ssd-mount-ok").touch()
    (ssd / ".qbsc-anomaly").touch()
    (ssd / "stray.txt").write_text("x", encoding="utf-8")

    found = {ih for ih, _ in recovery.iter_ssd_infohash_dirs(ssd)}
    assert found == {"HASHA", "HASHB"}


def test_ssd_dir_payload_helpers_ignore_sidecar(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    d = ssd / "HASH"
    d.mkdir(parents=True)
    # Only a sidecar -> no payload.
    recovery.write_meta(ssd, infohash="HASH", since_ts=1, ssd_bytes=0, bulk_targets={"a": "b"})
    assert recovery.ssd_dir_has_payload(d) is False
    assert recovery.ssd_dir_bytes(d) == 0

    # Add a real file.
    (d / "movie.mkv").write_bytes(b"x" * 123)
    assert recovery.ssd_dir_has_payload(d) is True
    assert recovery.ssd_dir_bytes(d) == 123


def test_anomaly_marker_set_clear(tmp_path: Path) -> None:
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    assert recovery.has_anomaly(ssd) is False
    recovery.set_anomaly(ssd, "boom")
    assert recovery.has_anomaly(ssd) is True
    assert (ssd / recovery.ANOMALY_MARKER).read_text(encoding="utf-8") == "boom"
    recovery.clear_anomaly(ssd)
    assert recovery.has_anomaly(ssd) is False


def test_healthcheck_unhealthy_when_anomaly(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    monkeypatch.setenv("QBSC_SSD_DIR", str(ssd))

    assert healthcheck.main() == 0
    recovery.set_anomaly(ssd, "ssd dir without sidecar or DB mapping: HASH")
    assert healthcheck.main() == 1
    recovery.clear_anomaly(ssd)
    assert healthcheck.main() == 0
