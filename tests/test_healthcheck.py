"""healthcheck: liveness only — a data anomaly must not fail it by default.

Regression for the production restart loop: the anomaly marker made the
container unhealthy, an autoheal sidecar restarted it every ~3 minutes, and
every restart killed (and later threw away) an in-flight 20 GB promotion.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from qbittorrent_seed_cache import healthcheck, recovery


@pytest.fixture
def ssd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "ssd"
    d.mkdir()
    monkeypatch.setenv("QBSC_SSD_DIR", str(d))
    monkeypatch.delenv(healthcheck.FAIL_ON_ANOMALY_ENV, raising=False)
    return d


def test_missing_ssd_dir_is_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBSC_SSD_DIR", str(tmp_path / "nope"))
    assert healthcheck.main() == 1


def test_healthy_without_heartbeat(ssd: Path) -> None:
    # Daemon still starting (or an older daemon without heartbeats).
    assert healthcheck.main() == 0


def test_fresh_heartbeat_is_healthy(ssd: Path) -> None:
    now = int(time.time())
    recovery.write_heartbeat(ssd, now_ts=now, stale_after_ts=now + 600)
    assert healthcheck.main() == 0


def test_stale_heartbeat_is_unhealthy(ssd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = int(time.time())
    recovery.write_heartbeat(ssd, now_ts=now - 2000, stale_after_ts=now - 10)
    assert healthcheck.main() == 1
    assert "heartbeat stale" in capsys.readouterr().err


def test_anomaly_is_reported_but_healthy_by_default(
    ssd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recovery.set_anomaly(ssd, "1 torrent(s), 2 symlink(s) point into the SSD ...\nHASH\tlink")
    assert healthcheck.main() == 0
    out = capsys.readouterr().out
    assert "DATA ANOMALY" in out
    assert "1 torrent(s)" in out


def test_anomaly_fails_when_opted_in(
    ssd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery.set_anomaly(ssd, "boom")
    monkeypatch.setenv(healthcheck.FAIL_ON_ANOMALY_ENV, "1")
    assert healthcheck.main() == 1
    recovery.clear_anomaly(ssd)
    assert healthcheck.main() == 0


def test_stale_heartbeat_wins_over_anomaly(ssd: Path) -> None:
    now = int(time.time())
    recovery.set_anomaly(ssd, "boom")
    recovery.write_heartbeat(ssd, now_ts=now - 2000, stale_after_ts=now - 1)
    assert healthcheck.main() == 1
