"""Structured logging setup for structlog.

Centralises structlog configuration so every entry point (and the tests) shares
one deterministic setup. The log *level* is applied through a filtering
wrapper, because the installed structlog does not accept a ``level`` keyword on
:class:`structlog.configure` (doing so raises ``TypeError`` at startup).
"""

from __future__ import annotations

import logging

import structlog


def _level_number(level: str) -> int:
    """Map a level name (``"INFO"``) to a ``logging`` number, tolerating ints."""
    if isinstance(level, int):
        return level
    name = level.upper() if isinstance(level, str) else "INFO"
    return getattr(logging, name, logging.INFO)


def setup_logging(level: str = "INFO") -> None:
    """Configure structlog for console output at the given level.

    Safe to call multiple times — structlog keeps a single global configuration.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_number(level)),
        cache_logger_on_first_use=True,
    )
