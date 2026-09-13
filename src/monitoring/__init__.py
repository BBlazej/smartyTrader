"""Monitoring — structured logging + alert dispatching.

- :mod:`monitoring.logger` — shared structlog setup (level-aware, no startup crash).
- :mod:`monitoring.alerts` — :class:`AlertManager` + :class:`AlertSink` protocol.
"""

from __future__ import annotations

from .alerts import AlertManager, AlertSink, NoopAlertSink, TelegramAlertSink
from .logger import setup_logging

__all__ = ["AlertManager", "AlertSink", "NoopAlertSink", "TelegramAlertSink", "setup_logging"]
