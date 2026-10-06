"""CLI entrypoint.

    qbittorrent-seed-cache [-c CONFIG] [--dry-run]          # run the daemon
    qbittorrent-seed-cache [-c CONFIG] repair-dangling ...  # diagnose/repair
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .logging_setup import configure_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="qbittorrent-seed-cache")
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(os.environ.get("QBSC_CONFIG", "/etc/qbittorrent-seed-cache/config.yaml")),
        help="Path to YAML config file.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Override config: log intended actions but do not touch the filesystem.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="Run the daemon (default when no command is given).")
    r = sub.add_parser(
        "repair-dangling",
        help="List (and with --apply, repair) symlinks into the SSD whose "
        "link->bulk mapping is lost. Read-only by default.",
        description="Find qB symlinks pointing into the SSD cache with no known "
        "bulk mapping, locate the bulk file by size and verify its content "
        "against the torrent's piece hashes. Dry run unless --apply.",
    )
    r.add_argument(
        "--apply",
        action="store_true",
        help="Retarget links with exactly one content-verified candidate to that bulk file.",
    )
    r.add_argument(
        "--recheck",
        action="store_true",
        help="With --apply: ask qB to recheck torrents whose links were all repaired.",
    )
    r.add_argument("--json", action="store_true", help="Machine-readable output.")
    r.add_argument(
        "--infohash", action="append", default=[], help="Limit to this infohash (repeatable)."
    )
    r.add_argument(
        "--search-root",
        action="append",
        type=Path,
        default=[],
        help="Where to look for bulk candidates (repeatable; default: managed_paths).",
    )
    r.add_argument(
        "--verify-pieces",
        type=int,
        default=3,
        help="Pieces hashed per candidate (default 3).",
    )
    r.add_argument("-v", "--verbose", action="store_true", help="Log progress to stderr.")
    args = p.parse_args(argv)
    if getattr(args, "recheck", False) and not args.apply:
        p.error("--recheck requires --apply")
    if getattr(args, "verify_pieces", 1) < 1:
        p.error("--verify-pieces must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config: Config = load_config(args.config)

    if args.command == "repair-dangling":
        from .repair import run_repair

        # Logs go to stderr so stdout stays a clean table / JSON document.
        configure_logging("INFO" if args.verbose else "WARNING", "console", stream=sys.stderr)
        return run_repair(
            config,
            apply=args.apply,
            recheck=args.recheck,
            as_json=args.json,
            infohashes=args.infohash,
            search_roots=args.search_root or None,
            verify_pieces=args.verify_pieces,
        )

    if args.dry_run:
        config = config.model_copy(update={"dry_run": True})

    configure_logging(config.log_level, config.log_format)

    from .daemon import run_daemon

    try:
        asyncio.run(run_daemon(config))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
