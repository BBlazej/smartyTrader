"""Structured logging setup for structlog.

Centralises structlog configuration so every entry point (and the tests) shares
one deterministic setup. The log *level* is applied through a filtering
wrapper, because the installed structlog does not accept a ``level`` keyword on
:class:`structlog.configure` (doing so raises ``TypeError`` at startup).
"""

from __future__ import annotations

import logging

import structlog
import structlog.types

#: Human-readable local wall clock — log files are read on the machine that wrote them.
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _level_number(level: str) -> int:
    """Map a level name (``"INFO"``) to a ``logging`` number, tolerating ints."""
    if isinstance(level, int):
        return level
    name = level.upper() if isinstance(level, str) else "INFO"
    return getattr(logging, name, logging.INFO)


def _needs_quoting(text: str) -> bool:
    return not text or any(char.isspace() for char in text)


def _render_console_line(
    _logger: object, method_name: str, event_dict: structlog.types.EventDict
) -> str:
    """Render one line as ``[YYYY-MM-DD HH:MM:SS][level] message key=value ...``.

    Values containing whitespace are quoted (newlines escaped) so every record
    stays grep-able on one line; a formatted traceback (if any) is appended raw
    after the line, where multi-line readability wins.
    """
    timestamp = event_dict.pop("timestamp", "-")
    level = event_dict.pop("log_level", method_name)
    event = str(event_dict.pop("event", ""))
    exception = event_dict.pop("exception", None)
    parts = []
    for key, value in event_dict.items():
        text = str(value)
        parts.append(f"{key}={text!r}" if _needs_quoting(text) else f"{key}={text}")
    line = f"[{timestamp}][{level}] {event}"
    if parts:
        line += " " + " ".join(parts)
    if exception:
        line += f"\n{exception}"
    return line


def setup_logging(level: str = "INFO") -> None:
    """Configure structlog console output at the given level.

    Every line is prefixed ``[timestamp][level]`` (local wall clock, see
    :data:`LOG_TIME_FORMAT`), followed by the message and its key/value context.

    Safe to call multiple times — structlog keeps a single global configuration.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt=LOG_TIME_FORMAT, utc=False, key="timestamp"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _render_console_line,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_number(level)),
        cache_logger_on_first_use=True,
    )
