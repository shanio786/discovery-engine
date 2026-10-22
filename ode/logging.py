"""structlog configuration. Call `configure()` once at process start."""

from __future__ import annotations

import logging
import sys

import structlog


def configure(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure structlog + stdlib logging.

    json_logs=True in production (parseable); human-readable otherwise.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
