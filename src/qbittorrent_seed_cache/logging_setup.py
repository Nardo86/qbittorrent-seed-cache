"""structlog configuration."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

import structlog


def configure_logging(level: str, fmt: str, *, stream: TextIO | None = None) -> None:
    out = stream if stream is not None else sys.stdout
    logging.basicConfig(
        format="%(message)s",
        stream=out,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # httpx logs every request at INFO ("HTTP Request: GET ..."): one line per
    # torrent per tick. Keep only its warnings.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLogger().level))

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.PrintLoggerFactory(file=out),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
