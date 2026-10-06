"""repair-dangling: content-verified recovery of links with a lost mapping."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from qbittorrent_seed_cache import repair
from qbittorrent_seed_cache.__main__ import parse_args
from qbittorrent_seed_cache.qbit_client import TorrentInfo
from qbittorrent_seed_cache.state import StateStore

from .test_tick_integration import make_config, make_dirs

PS = 1024  # piece size


def _content(seed: int, n: int) -> bytes:
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}-{i}".encode()).digest()
        i += 1
    return out[:n]


def _piece_hashes(data: bytes) -> list[str]:
    return [hashlib.sha1(data[i : i + PS]).hexdigest() for i in range(0, len(data), PS)]


class _World:
    """Bulk library + torrents dir + a fake qB serving files/pieces."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bulk_root, self.ssd = make_dirs(tmp_path)
        self.storage = self.bulk_root / "storage"
        self.torrents: list[TorrentInfo] = []
        self.files: dict[str, list[dict[str, Any]]] = {}
        self.hashes: dict[str, list[str]] = {}
        self.rechecked: list[str] = []
        self.config = make_config(
            tmp_path, bulk_root=self.bulk_root, ssd=self.ssd, instance_names=["qb1"]
        )
        world = self

        class _Fake:
            def __init__(self, **_kw: Any) -> None:
                pass

            async def __aenter__(self) -> _Fake:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def torrents(self) -> list[TorrentInfo]:
                return list(world.torrents)

            async def torrent_files(self, h: str) -> list[dict[str, Any]]:
                return world.files[h]

            async def torrent_properties(self, h: str) -> dict[str, Any]:
                return {"piece_size": PS}

            async def piece_hashes(self, h: str) -> list[str]:
                return world.hashes[h]

            async def recheck(self, hashes: list[str]) -> None:
                world.rechecked.extend(hashes)

        monkeypatch.setattr(repair, "QbitClient", _Fake)

    def library_file(self, rel: str, data: bytes) -> Path:
        p = self.storage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def torrent(self, ih: str, files: list[tuple[str, bytes]]) -> list[Path]:
        """Register a torrent whose links all dangle into a missing SSD dir."""
        save = self.storage / "torrents" / f"rel-{ih}"
        blob = b"".join(d for _, d in files)
        api, links, off = [], [], 0
        for idx, (name, data) in enumerate(files):
            link = save / name
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(self.ssd / ih / name, link)
            links.append(link)
            first, last = off // PS, (off + len(data) - 1) // PS
            api.append({"index": idx, "name": name, "size": len(data), "piece_range": [first, last]})
            off += len(data)
        self.files[ih] = api
        self.hashes[ih] = _piece_hashes(blob)
        self.torrents.append(
            TorrentInfo(
                hash=ih, name=ih, save_path=f"/data/torrents/rel-{ih}",
                content_path=f"/data/torrents/rel-{ih}", size=len(blob), upspeed=0,
                uploaded_session=0, last_activity=int(time.time()), state="missingFiles",
            )
        )
        return links

    def run(self, **kw: Any) -> list[dict[str, Any]]:
        out = io.StringIO()
        repair.run_repair(self.config, as_json=True, out=out, **kw)
        rows: list[dict[str, Any]] = json.loads(out.getvalue())
        return rows


def test_dry_run_finds_verified_candidate_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _World(tmp_path, monkeypatch)
    data = _content(1, 10 * PS + 17)
    bulk = w.library_file("Films/Dune (2024)/Dune (2024) [WEBDL].mkv", data)
    (link,) = w.torrent("AAA", [("Dune.Parte.Due.mkv", data)])
    before = os.readlink(link)

    rows = w.run()
    assert [(r["status"], r["candidate"]) for r in rows] == [("verified", str(bulk))]
    assert rows[0]["target_exists"] is False
    assert os.readlink(link) == before  # dry run


def test_apply_retargets_relative_and_rechecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _World(tmp_path, monkeypatch)
    data = _content(2, 8 * PS)
    bulk = w.library_file("Films/X/x.mkv", data)
    (link,) = w.torrent("BBB", [("x.release.mkv", data)])

    rows = w.run(apply=True, recheck=True)
    assert rows[0]["status"] == "repaired"
    assert not Path(os.readlink(link)).is_absolute()
    assert link.resolve() == bulk.resolve()
    assert link.read_bytes() == data
    assert bulk.read_bytes() == data  # real file untouched
    assert w.rechecked == ["BBB"]


def test_same_size_different_content_is_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _World(tmp_path, monkeypatch)
    data = _content(3, 6 * PS)
    w.library_file("Films/Other/other.mkv", _content(99, 6 * PS))
    (link,) = w.torrent("CCC", [("c.mkv", data)])
    before = os.readlink(link)
    rows = w.run(apply=True, recheck=True)
    assert rows[0]["status"] == "mismatch"
    assert os.readlink(link) == before
    assert w.rechecked == []


def test_no_candidate_and_unverifiable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(tmp_path, monkeypatch)
    w.torrent("DDD", [("gone.mkv", _content(4, 5 * PS))])
    small = _content(5, 100)  # smaller than one piece
    w.library_file("Films/S/s.srt", small)
    w.torrent("EEE", [("s.srt", small)])
    rows = {r["infohash"]: r["status"] for r in w.run(apply=True)}
    assert rows == {"DDD": "no_candidate", "EEE": "unverifiable"}


def test_two_distinct_identical_files_are_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _World(tmp_path, monkeypatch)
    data = _content(6, 4 * PS)
    w.library_file("Films/A/a.mkv", data)
    w.library_file("Films/B/b.mkv", data)
    (link,) = w.torrent("FFF", [("f.mkv", data)])
    before = os.readlink(link)
    rows = w.run(apply=True)
    assert rows[0]["status"] == "ambiguous"
    assert os.readlink(link) == before


def test_library_copy_wins_over_real_copy_in_a_save_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production case: the library file and a separate real copy in the other
    qB instance's save dir are identical; the library file is chosen."""
    w = _World(tmp_path, monkeypatch)
    data = _content(13, 4 * PS)
    lib = w.library_file("Films/Fireworks (2023)/Fireworks (2023).mkv", data)
    w.torrent("SSS", [("Stranizza.mkv", data)])
    # Another torrent (other instance) whose save dir holds a real copy.
    other_save = w.storage / "torrents" / "ArabaFenice"
    other_save.mkdir(parents=True)
    (other_save / "Stranizza.mkv").write_bytes(data)
    w.torrents.append(
        TorrentInfo(hash="OTHER", name="o", save_path="/data/torrents/ArabaFenice",
                    content_path="/data/torrents/ArabaFenice", size=len(data), upspeed=0,
                    uploaded_session=0, last_activity=0, state="uploading")
    )
    w.files["OTHER"] = [{"index": 0, "name": "Stranizza.mkv", "size": len(data)}]
    rows = w.run()
    assert (rows[0]["status"], rows[0]["candidate"]) == ("verified", str(lib))
    assert "identical copy also at" in rows[0]["detail"]


def test_hardlinks_count_once_and_library_path_preferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _World(tmp_path, monkeypatch)
    data = _content(7, 4 * PS)
    bulk = w.library_file("Films/H/h.mkv", data)
    (link,) = w.torrent("GGG", [("h.mkv", data)])
    os.link(bulk, link.parent / "hardlink-copy.mkv")  # same inode inside the save dir
    rows = w.run()
    assert (rows[0]["status"], rows[0]["candidate"]) == ("verified", str(bulk))


def test_multi_file_torrent_offsets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files after the first start mid-piece; verification must use the right offset."""
    w = _World(tmp_path, monkeypatch)
    e1, e2, e3 = _content(8, 3 * PS + 300), _content(9, 5 * PS + 11), _content(10, 4 * PS)
    b1 = w.library_file("SerieTV/S/S01E01.mkv", e1)
    b2 = w.library_file("SerieTV/S/S01E02.mkv", e2)
    b3 = w.library_file("SerieTV/S/S01E03.mkv", e3)
    w.torrent("HHH", [("S/e1.mkv", e1), ("S/e2.mkv", e2), ("S/e3.mkv", e3)])
    rows = {Path(r["link"]).name: (r["status"], r["candidate"]) for r in w.run()}
    assert rows == {
        "e1.mkv": ("verified", str(b1)),
        "e2.mkv": ("verified", str(b2)),
        "e3.mkv": ("verified", str(b3)),
    }


def test_mapped_hot_links_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(tmp_path, monkeypatch)
    data = _content(11, 4 * PS)
    bulk = w.library_file("Films/K/k.mkv", data)
    (link,) = w.torrent("KKK", [("k.mkv", data)])
    store = StateStore(w.config.state_db)
    store.set_tier(infohash="KKK", tier="hot", since_ts=1, ssd_bytes=1,
                   bulk_targets={str(link): str(bulk)})
    store.close()
    assert w.run() == []


def test_link_changed_after_scan_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _World(tmp_path, monkeypatch)
    data = _content(12, 4 * PS)
    w.library_file("Films/L/l.mkv", data)
    (link,) = w.torrent("LLL", [("l.mkv", data)])
    result = __import__("asyncio").run(repair.scan(w.config))
    repair.classify(w.config, result)
    os.unlink(link)
    os.symlink("/elsewhere.mkv", link)  # someone fixed it by hand meanwhile
    assert repair.apply_repairs(result.findings) == 0
    assert result.findings[0].status == "changed_since_scan"
    assert os.readlink(link) == "/elsewhere.mkv"


def test_cli_parsing() -> None:
    a = parse_args(["repair-dangling"])
    assert a.command == "repair-dangling" and a.apply is False
    assert parse_args([]).command is None  # daemon stays the default
    with pytest.raises(SystemExit):
        parse_args(["repair-dangling", "--recheck"])
