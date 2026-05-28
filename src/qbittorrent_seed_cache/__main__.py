"""CLI entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .daemon import run_daemon
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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config: Config = load_config(args.config)
    if args.dry_run:
        config = config.model_copy(update={"dry_run": True})

    configure_logging(config.log_level, config.log_format)

    try:
        asyncio.run(run_daemon(config))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
