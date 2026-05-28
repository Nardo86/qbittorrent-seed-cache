#!/usr/bin/env python3
"""One-shot migration: convert hardlinks in torrents/ to relative symlinks
pointing at the media library copy.

Builds an inode->paths map by walking the media library roots, then walks
torrents/ and for every regular file whose inode is also present in the
media library, replaces it atomically with a relative symlink.

Files in torrents/ whose inode is NOT in the media library (link count 1,
or hardlinked elsewhere only) are left untouched.

Example:
    migrate-hardlinks-to-symlinks.py \\
        --torrents-root /mnt/media/storage/torrents \\
        --media-root    /mnt/media/storage/Movies   \\
        --media-root    /mnt/media/storage/TV       \\
        --apply
"""

import argparse
import os
import sys
import time
from pathlib import Path


def build_inode_map(roots: list[Path]) -> dict[int, list[Path]]:
    inode_map: dict[int, list[Path]] = {}
    for root in roots:
        if not root.exists():
            print(f"  skip (not found): {root}")
            continue
        n = 0
        for f in root.rglob("*"):
            try:
                if not f.is_file() or f.is_symlink():
                    continue
                st = f.stat()
            except OSError:
                continue
            inode_map.setdefault(st.st_ino, []).append(f)
            n += 1
        print(f"  scanned {root}: {n} files")
    return inode_map


def replace_atomically(path: Path, rel_target: str) -> None:
    tmp = path.with_name(path.name + ".symtmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(rel_target, tmp)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--torrents-root", type=Path, required=True,
                    help="root of the qBittorrent download/seed directory to migrate")
    ap.add_argument("--media-root", type=Path, action="append", required=True,
                    dest="media_roots", metavar="PATH",
                    help="media library subtree to scan for hardlink targets "
                         "(repeat for multiple roots, e.g. Movies, TV)")
    ap.add_argument("--apply", action="store_true",
                    help="actually perform the migration (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N candidates (0 = no limit)")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] building inode map from media library...")
    t0 = time.time()
    inode_map = build_inode_map(args.media_roots)
    print(f"  total unique inodes: {len(inode_map)} ({time.time()-t0:.1f}s)")
    print()

    torrents_root: Path = args.torrents_root
    print(f"[{mode}] scanning {torrents_root}...")
    stats = {
        "files": 0,
        "symlink": 0,
        "single_link": 0,
        "no_media_match": 0,
        "migrated": 0,
        "errors": 0,
    }
    t0 = time.time()
    for f in torrents_root.rglob("*"):
        if "/.Trash" in str(f):
            continue
        try:
            if f.is_symlink():
                stats["symlink"] += 1
                continue
            if not f.is_file():
                continue
            st = f.stat()
        except OSError:
            continue
        stats["files"] += 1
        if st.st_nlink < 2:
            stats["single_link"] += 1
            continue
        targets = inode_map.get(st.st_ino, [])
        if not targets:
            stats["no_media_match"] += 1
            continue
        target = targets[0]
        rel = os.path.relpath(target, f.parent)
        if args.apply:
            try:
                replace_atomically(f, rel)
                stats["migrated"] += 1
            except OSError as e:
                print(f"  ERR: {f}: {e}", file=sys.stderr)
                stats["errors"] += 1
        else:
            stats["migrated"] += 1
            if stats["migrated"] <= 10:
                print(f"  WOULD: {f}")
                print(f"     -> {rel}")
        if args.limit and stats["migrated"] >= args.limit:
            break

    print()
    print(f"[{mode}] done in {time.time()-t0:.1f}s")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
