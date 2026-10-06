"""``repair-dangling``: find (and optionally fix) SSD symlinks with a lost mapping.

The anomaly this addresses: a qB symlink under ``torrents/`` points into
``<ssd_cache_dir>/<infohash>/...`` (often into a directory that no longer
exists), but neither the DB nor a sidecar remembers which bulk file it stood
for. The daemon cannot demote it, and qB shows the torrent as
``missingFiles``.

Mapping recovery
----------------
The bulk file is still in the media library (Sonarr/Radarr imported it;
the torrent link was only a symlink to it), usually under a different name.
For each such link we:

1. take the file's exact size from qB's file list and look for regular
   files of that size under the search roots (default: ``managed_paths``;
   the SSD cache and symlinks are ignored; hard links count once);
2. **verify the content** of every same-size candidate against the
   torrent's own SHA-1 piece hashes (qB ``pieceHashes``): a few pieces that
   lie entirely inside the file are read from the candidate and hashed. A
   match is cryptographic evidence that the candidate *is* that torrent
   file. The file's offset inside the torrent is derived from qB's file
   order/sizes and cross-checked with ``piece_range`` (piece-aligned
   offsets are tried too, for torrents with padding files);
3. classify: ``verified`` (exactly one content-verified file), or one of
   ``ambiguous`` (several distinct files verify and not exactly one of
   them is outside the qB save dirs, i.e. in the media library), ``mismatch`` (same size,
   different content), ``unverifiable`` (no whole piece inside the file,
   or a v2-only torrent), ``no_candidate``, ``too_many_candidates``.

Safety
------
* Dry run by default: prints the ``link -> candidate`` table, changes
  nothing. ``--apply`` retargets **only** ``verified`` links, to a relative
  symlink into the bulk fs — exactly what ``retarget_to_bulk`` does.
* Nothing is ever deleted; real files are never touched (the retarget
  refuses anything that is not a symlink) and a link that changed since the
  scan is skipped.
* The DB is opened read-only. The daemon notices the repaired links on its
  next tick (they now resolve into bulk, i.e. cold) and clears the marker.
* ``--recheck`` (with ``--apply``) asks qB to recheck the torrents whose
  unmapped links were all repaired, so they leave ``missingFiles``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

import structlog

from . import recovery
from .config import Config
from .paths import map_to_host
from .qbit_client import QbitClient
from .resolver import is_under
from .state import StateStore
from .symlinks import atomic_retarget, relative_target

log = structlog.get_logger(__name__)

VERIFIED = "verified"
REPAIRED = "repaired"
AMBIGUOUS = "ambiguous"
MISMATCH = "mismatch"
UNVERIFIABLE = "unverifiable"
NO_CANDIDATE = "no_candidate"
TOO_MANY = "too_many_candidates"
UNMANAGED = "unmanaged"
CHANGED = "changed_since_scan"
FAILED = "failed"

MAX_CANDIDATES = 16


@dataclass
class Finding:
    """One symlink into the SSD whose link->bulk mapping is unknown."""

    instance: str
    infohash: str
    torrent: str
    link: str
    target: str  # raw readlink() value, re-checked before --apply
    target_exists: bool
    size: int
    status: str = ""
    candidate: str | None = None
    detail: str = ""
    # Internal: file offset hypotheses inside the torrent + qB file index.
    offsets: tuple[int, ...] = field(default=(), repr=False)
    file_index: int = field(default=0, repr=False)


@dataclass(frozen=True, slots=True)
class PieceInfo:
    piece_size: int
    hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    dev: int
    ino: int


@dataclass
class ScanResult:
    findings: list[Finding]
    pieces: dict[str, PieceInfo]
    save_roots: set[Path]


# --- scan -------------------------------------------------------------------------


def _offset_hypotheses(files: list[dict[str, Any]], pos: int, piece_size: int) -> tuple[int, ...]:
    """Candidate byte offsets of ``files[pos]`` inside the torrent's data."""
    indexed = sorted(
        ((int(f.get("index", i)), int(f["size"])) for i, f in enumerate(files)),
    )
    offsets: dict[int, int] = {}
    off = 0
    for idx, size in indexed:
        offsets[idx] = off
        off += size
    f = files[pos]
    size = int(f["size"])
    idx = int(f.get("index", pos))
    hyps = [offsets[idx]]
    raw = f.get("piece_range")
    pr: tuple[int, int] | None = None
    if isinstance(raw, list) and len(raw) == 2 and piece_size > 0:
        pr = (int(raw[0]), int(raw[1]))
        hyps.append(pr[0] * piece_size)
    out: list[int] = []
    for h in dict.fromkeys(hyps):
        if pr is not None and size > 0 and (
            h // piece_size != pr[0] or (h + size - 1) // piece_size != pr[1]
        ):
            continue
        out.append(h)
    return tuple(out)


def _known_links(
    config: Config, maps: dict[str, dict[str, str]], cache: dict[str, set[str]], infohash: str
) -> set[str]:
    if infohash not in cache:
        known = set(maps.get(infohash, {}))
        meta = recovery.read_meta(config.ssd_cache_dir, infohash)
        if meta is not None:
            known |= set(meta.bulk_targets)
        cache[infohash] = known
    return cache[infohash]


async def scan(config: Config, *, only: set[str] | None = None) -> ScanResult:
    """Poll every qB instance and list SSD links with no known mapping."""
    maps: dict[str, dict[str, str]] = {}
    if config.state_db.exists():
        store = StateStore(config.state_db, readonly=True)
        try:
            maps = store.hot_bulk_maps()
        finally:
            store.close()

    known_cache: dict[str, set[str]] = {}
    findings: list[Finding] = []
    pieces: dict[str, PieceInfo] = {}
    save_roots: set[Path] = set()
    ssd = config.ssd_cache_dir

    for instance in config.instances:
        async with QbitClient(
            name=instance.name,
            url=instance.url,
            username=instance.username,
            password=instance.password.get_secret_value(),
        ) as client:
            for t in await client.torrents():
                save_host = map_to_host(t.save_path, instance.path_map)
                save_roots.add(save_host)
                if only and t.hash not in only:
                    continue
                files = await client.torrent_files(t.hash)
                hits: list[tuple[int, Path]] = []
                for pos, f in enumerate(files):
                    link = save_host / f["name"]
                    if not link.is_symlink():
                        continue
                    if not is_under(Path(os.path.realpath(link)), ssd):
                        continue
                    if str(link) in _known_links(config, maps, known_cache, t.hash):
                        continue
                    hits.append((pos, link))
                if not hits:
                    continue
                if t.hash not in pieces:
                    props = await client.torrent_properties(t.hash)
                    pieces[t.hash] = PieceInfo(
                        piece_size=int(props.get("piece_size") or 0),
                        hashes=tuple(await client.piece_hashes(t.hash)),
                    )
                ps = pieces[t.hash].piece_size
                for pos, link in hits:
                    f = files[pos]
                    findings.append(
                        Finding(
                            instance=instance.name,
                            infohash=t.hash,
                            torrent=t.name,
                            link=str(link),
                            target=os.readlink(link),
                            target_exists=link.exists(),
                            size=int(f["size"]),
                            offsets=_offset_hypotheses(files, pos, ps),
                            file_index=int(f.get("index", pos)),
                        )
                    )
    return ScanResult(findings=findings, pieces=pieces, save_roots=save_roots)


# --- candidates & verification ---------------------------------------------------


def _index_by_size(
    roots: Iterable[Path], wanted: set[int], exclude: list[Path], save_roots: set[Path]
) -> dict[int, list[_Candidate]]:
    """Regular files (not symlinks) whose size is in ``wanted``, one per inode."""
    by_inode: dict[tuple[int, int], _Candidate] = {}

    def rank(p: Path) -> tuple[bool, int, str]:
        # Prefer the media-library path over a hard link inside a qB save dir.
        return (any(is_under(p, r) for r in save_roots), len(str(p)), str(p))

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not any(is_under(here / d, e) for e in exclude)]
            for name in filenames:
                p = here / name
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size not in wanted:
                    continue
                key = (st.st_dev, st.st_ino)
                prev = by_inode.get(key)
                if prev is None or rank(p) < rank(prev.path):
                    by_inode[key] = _Candidate(path=p, dev=st.st_dev, ino=st.st_ino)

    out: dict[int, list[_Candidate]] = {}
    for c in by_inode.values():
        out.setdefault(c.path.stat().st_size, []).append(c)
    for lst in out.values():
        lst.sort(key=lambda c: str(c.path))
    return out


def _sample_pieces(offset: int, size: int, piece: PieceInfo, k: int) -> list[int]:
    """Up to ``k`` pieces lying entirely inside ``[offset, offset+size)``."""
    ps = piece.piece_size
    if ps <= 0:
        return []
    first = -(-offset // ps)
    last = min((offset + size) // ps - 1, len(piece.hashes) - 1)
    if last < first:
        return []
    count = last - first + 1
    if count <= k:
        return list(range(first, last + 1))
    if k == 1:
        return [first + count // 2]
    return sorted({first + round(j * (count - 1) / (k - 1)) for j in range(k)})


def _matches(path: Path, offset: int, piece: PieceInfo, sample: list[int]) -> bool:
    ps = piece.piece_size
    try:
        with path.open("rb") as fh:
            for p in sample:
                fh.seek(p * ps - offset)
                data = fh.read(ps)
                if len(data) != ps or hashlib.sha1(data).hexdigest() != piece.hashes[p].lower():
                    return False
    except OSError:
        return False
    return True


def _is_sha1(h: str) -> bool:
    return len(h) == 40 and all(c in "0123456789abcdefABCDEF" for c in h)


def classify(
    config: Config,
    result: ScanResult,
    *,
    search_roots: list[Path] | None = None,
    verify_pieces: int = 3,
) -> None:
    """Fill ``status``/``candidate``/``detail`` of every finding (reads the bulk)."""
    if not result.findings:
        return
    roots = search_roots or list(config.managed_paths)
    index = _index_by_size(
        roots,
        {f.size for f in result.findings},
        [config.ssd_cache_dir],
        result.save_roots,
    )
    verdicts: dict[tuple[str, int, int, int], bool] = {}
    for f in result.findings:
        cands = index.get(f.size, [])
        if not cands:
            f.status, f.detail = NO_CANDIDATE, f"no regular file of {f.size} bytes"
            continue
        if len(cands) > MAX_CANDIDATES:
            f.status, f.detail = TOO_MANY, f"{len(cands)} files of {f.size} bytes"
            continue
        piece = result.pieces[f.infohash]
        plans = [
            (off, sample)
            for off in f.offsets
            if (sample := _sample_pieces(off, f.size, piece, verify_pieces))
            and all(_is_sha1(piece.hashes[p]) for p in sample)
        ]
        if not plans:
            f.status = UNVERIFIABLE
            f.detail = f"{len(cands)} same-size file(s) but no whole SHA-1 piece inside the file"
            continue
        verified: list[_Candidate] = []
        for c in cands:
            key = (f.infohash, f.file_index, c.dev, c.ino)
            if key not in verdicts:
                verdicts[key] = any(_matches(c.path, off, piece, s) for off, s in plans)
            if verdicts[key]:
                verified.append(c)
        tie_note = ""
        if len(verified) > 1:
            # Identical content in several distinct files — typically the
            # library file plus a real (non-symlinked) copy in another qB
            # instance's save dir. The convention is that torrent links point
            # into the media library, so if exactly one verified file lies
            # outside every qB save dir, that one is the answer.
            in_library = [
                c for c in verified if not any(is_under(c.path, r) for r in result.save_roots)
            ]
            if len(in_library) == 1:
                others = [str(c.path) for c in verified if c is not in_library[0]]
                tie_note = "; identical copy also at: " + ", ".join(others)
                verified = in_library
        if not verified:
            f.status, f.detail = MISMATCH, f"{len(cands)} same-size file(s), none matches"
        elif len(verified) > 1:
            f.status = AMBIGUOUS
            f.detail = "content matches: " + ", ".join(str(c.path) for c in verified)
        elif not any(is_under(verified[0].path, mp) for mp in config.managed_paths):
            f.status, f.candidate = UNMANAGED, str(verified[0].path)
            f.detail = "verified, but outside managed_paths"
        else:
            f.status, f.candidate = VERIFIED, str(verified[0].path)
            f.detail = f"{len(plans[0][1])} piece(s) verified{tie_note}"


# --- apply ---------------------------------------------------------------------------


def apply_repairs(findings: list[Finding]) -> int:
    """Retarget every ``verified`` link to its bulk file. Returns links repaired."""
    repaired = 0
    for f in findings:
        if f.status != VERIFIED or f.candidate is None:
            continue
        link = Path(f.link)
        try:
            if not link.is_symlink() or os.readlink(link) != f.target:
                f.status, f.detail = CHANGED, "link changed since the scan; left alone"
                continue
            rel = relative_target(link.parent, Path(f.candidate))
            atomic_retarget(link, rel)
        except (OSError, ValueError) as exc:
            f.status, f.detail = FAILED, str(exc)
            log.error("repair.retarget_failed", link=f.link, error=str(exc))
            continue
        log.info("repair.retarget", infohash=f.infohash, link=f.link, bulk=f.candidate)
        f.status = REPAIRED
        repaired += 1
    return repaired


async def recheck_repaired(config: Config, findings: list[Finding]) -> dict[str, list[str]]:
    """Ask qB to recheck torrents whose unmapped links were *all* repaired."""
    per_torrent: dict[tuple[str, str], set[str]] = {}
    for f in findings:
        per_torrent.setdefault((f.instance, f.infohash), set()).add(f.status)
    todo: dict[str, list[str]] = {}
    for (name, ih), statuses in sorted(per_torrent.items()):
        if statuses == {REPAIRED}:
            todo.setdefault(name, []).append(ih)
    for instance in config.instances:
        hashes = todo.get(instance.name)
        if not hashes:
            continue
        async with QbitClient(
            name=instance.name,
            url=instance.url,
            username=instance.username,
            password=instance.password.get_secret_value(),
        ) as client:
            await client.recheck(hashes)
        log.info("repair.recheck", instance=instance.name, torrents=len(hashes))
    return todo


# --- output --------------------------------------------------------------------------


def render(findings: list[Finding], out: TextIO, *, applied: bool) -> None:
    if not findings:
        print("No symlinks into the SSD with a lost mapping.", file=out)
        return
    print("# status\tinstance\tinfohash\tlink\tcandidate\tdetail", file=out)
    for f in sorted(findings, key=lambda f: (f.status, f.infohash, f.link)):
        missing = "" if f.target_exists else " [target missing]"
        print(
            f"{f.status}\t{f.instance}\t{f.infohash}\t{f.link}{missing}\t"
            f"{f.candidate or '-'}\t{f.detail}",
            file=out,
        )
    counts = Counter(f.status for f in findings)
    torrents = len({f.infohash for f in findings})
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"# {len(findings)} link(s) in {torrents} torrent(s): {summary}", file=out)
    if not applied and counts.get(VERIFIED):
        print(
            "# dry run: nothing changed. Re-run with --apply to retarget the "
            "'verified' links to their bulk file (add --recheck to recheck them in qB).",
            file=out,
        )


def run_repair(
    config: Config,
    *,
    apply: bool = False,
    recheck: bool = False,
    as_json: bool = False,
    infohashes: list[str] | None = None,
    search_roots: list[Path] | None = None,
    verify_pieces: int = 3,
    out: TextIO = sys.stdout,
) -> int:
    """CLI entry point. Returns the process exit code."""
    result = asyncio.run(scan(config, only=set(infohashes or []) or None))
    classify(config, result, search_roots=search_roots, verify_pieces=verify_pieces)
    if apply:
        apply_repairs(result.findings)
        if recheck:
            asyncio.run(recheck_repaired(config, result.findings))
    if as_json:
        rows = []
        for f in result.findings:
            row = asdict(f)
            row.pop("offsets")
            row.pop("file_index")
            rows.append(row)
        json.dump(rows, out, indent=2, ensure_ascii=False)
        print(file=out)
    else:
        render(result.findings, out, applied=apply)
    return 1 if any(f.status == FAILED for f in result.findings) else 0
