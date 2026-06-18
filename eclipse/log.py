"""Structured logging setup (console-friendly)."""

from __future__ import annotations

import logging

import structlog


def configure(verbose: bool = False) -> None:
    """Configure structlog for human-readable console output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="%(message)s", level=level)
    # httpx logs full request URLs at INFO — and the Telegram API puts the bot
    # token in the URL. Keep these at WARNING so secrets never reach the logs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
