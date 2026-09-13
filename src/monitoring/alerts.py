"""Alert dispatching — abstract sink + a deduplicating manager.

Alerts are notifications about *events* (an order filled, a signal rejected by
the risk gate, an LLM failure, an execution error). They are never used to make
trading decisions — the deterministic risk engine always runs first.

The manager is dependency-free (no network). The :class:`AlertSink` protocol is
the seam for a concrete delivery channel (email, webhooks, etc.); tests inject
a fake sink.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class AlertSink(Protocol):
    """A channel that delivers a single alert payload."""

    async def send(self, event: str, message: str, severity: str) -> bool: ...


class NoopAlertSink:
    """Default sink: logs the alert and pretends it was delivered."""

    async def send(self, event: str, message: str, severity: str) -> bool:
        logger.info(
            "alert (noop sink)", extra={"event": event, "severity": severity, "text": message}
        )
        return True


class AlertManager:
    """Fan-out to sinks with per-event deduplication.

    ``dedup_window`` seconds suppresses repeat alerts for the same ``(event,
    symbol)`` — so a held position being re-evaluated every cycle doesn't spam.
    """

    def __init__(
        self,
        sinks: list[AlertSink] | None = None,
        *,
        dedup_window: float = 300.0,
        clock=time.monotonic,
    ) -> None:
        self._sinks: list[AlertSink] = list(sinks) if sinks else [NoopAlertSink()]
        self._dedup_window = dedup_window
        self._clock = clock
        self._last_sent: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    @property
    def sinks(self) -> list[AlertSink]:
        return list(self._sinks)

    async def send(
        self, event: str, message: str, severity: str = "info", *, symbol: str | None = None
    ) -> bool:
        """Send ``event`` to every sink unless it was sent within the dedup window.

        Returns ``True`` if the alert was delivered (or deduplicated), ``False``
        only if every sink failed. A deduplicated alert counts as handled.
        """
        key = (event, symbol or "-")
        now = self._clock()
        async with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self._dedup_window:
                logger.debug("alert deduplicated", extra={"event": event, "symbol": symbol})
                return True
            self._last_sent[key] = now

        delivered = False
        for sink in self._sinks:
            try:
                if await sink.send(event, message, severity):
                    delivered = True
            except Exception as exc:  # noqa: BLE001 — one bad sink must not sink the rest
                logger.warning(
                    "alert sink failed",
                    extra={"event": event, "sink": type(sink).__name__, "error": str(exc)},
                )
        if not delivered:
            logger.error("all alert sinks failed", extra={"event": event, "symbol": symbol})
        return delivered

    def reset_dedup(self) -> None:
        """Clear the dedup state (useful between cycles/tests)."""
        self._last_sent.clear()
