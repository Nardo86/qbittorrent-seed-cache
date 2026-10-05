"""A demote must never strand symlinks it did not see this tick.

Hypothesis for how production ended up with links dangling into deleted SSD
dirs and no mapping anywhere: a torrent shared by two qB instances is
demoted while one instance's poll fails. Its score only counts the polled
instance, the demote retargets only the polled instance's links, `rm -rf`s
the shared SSD dir and resets the tier row (dropping `bulk_targets`) — the
other instance's links dangle with the mapping gone.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from qbittorrent_seed_cache.daemon import _tick
from qbittorrent_seed_cache.mover import TorrentLayout, demote
from qbittorrent_seed_cache.qbit_client import TorrentInfo
from qbittorrent_seed_cache.state import StateStore

from .test_tick_integration import make_config, make_dirs, make_fake_client


def _hot_shared(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    """Bulk file + SSD copy + one link per instance, both pointing into the SSD."""
    bulk_root, ssd = make_dirs(tmp_path)
    storage = bulk_root / "storage"
    bulk = storage / "Films" / "S" / "m.mkv"
    bulk.parent.mkdir(parents=True)
    bulk.write_bytes(b"x" * 100)
    (ssd / "S").mkdir()
    (ssd / "S" / "m.mkv").write_bytes(b"x" * 100)
    links = []
    for rel in ("rel-A", "rel-B"):
        d = storage / "torrents" / rel
        d.mkdir(parents=True)
        os.symlink(ssd / "S" / "m.mkv", d / "m.mkv")
        links.append(d / "m.mkv")
    return bulk_root, ssd, bulk, links[0], links[1]


def test_demote_retargets_persisted_links_missing_from_layouts(tmp_path: Path) -> None:
    _, ssd, bulk, link_a, link_b = _hot_shared(tmp_path)
    layout_a = TorrentLayout(
        instance="qb1", infohash="S", link=link_a, bulk_target=bulk, ssd_target=ssd / "S" / "m.mkv"
    )
    demote([layout_a], extra_bulk_targets={str(link_a): str(bulk), str(link_b): str(bulk)})

    assert not (ssd / "S").exists()
    for link in (link_a, link_b):
        assert not Path(os.readlink(link)).is_absolute()
        assert link.read_bytes() == b"x" * 100


def test_demote_extra_link_not_into_ssd_is_left_alone(tmp_path: Path) -> None:
    _, ssd, bulk, link_a, link_b = _hot_shared(tmp_path)
    os.unlink(link_b)
    os.symlink("/somewhere/else.mkv", link_b)  # user changed it; not ours anymore
    layout_a = TorrentLayout(
        instance="qb1", infohash="S", link=link_a, bulk_target=bulk, ssd_target=ssd / "S" / "m.mkv"
    )
    demote([layout_a], extra_bulk_targets={str(link_b): str(bulk)})
    assert os.readlink(link_b) == "/somewhere/else.mkv"


async def test_no_demotion_when_an_instance_poll_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk_root, ssd, bulk, link_a, link_b = _hot_shared(tmp_path)
    ti = TorrentInfo(
        hash="S", name="rel-A", save_path="/data/torrents/rel-A",
        content_path="/data/torrents/rel-A", size=100, upspeed=0, uploaded_session=0,
        last_activity=int(time.time()), state="uploading",
    )
    fake = make_fake_client({"qb1": {"torrents": [ti], "files": {"S": [{"name": "m.mkv", "size": 100}]}}})

    class _Down(fake):  # type: ignore[misc,valid-type]
        def __init__(self, *, name: str, **kw: Any) -> None:
            if name == "qb2":
                raise ConnectionError("qb2 down")
            super().__init__(name=name, **kw)

    monkeypatch.setattr("qbittorrent_seed_cache.daemon.QbitClient", _Down)
    config = make_config(tmp_path, bulk_root=bulk_root, ssd=ssd, instance_names=["qb1", "qb2"])
    store = StateStore(config.state_db)
    try:
        store.set_tier(infohash="S", tier="hot", since_ts=1, ssd_bytes=100,
                       bulk_targets={str(link_a): str(bulk), str(link_b): str(bulk)})
        await _tick(config, store)  # zero upload → would be demoted with a full poll
        tier = store.get_tier(infohash="S")
        assert tier is not None and tier.tier == "hot"
        assert (ssd / "S" / "m.mkv").is_file()
        assert link_b.read_bytes() == b"x" * 100
    finally:
        store.close()
