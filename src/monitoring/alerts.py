"""Alert dispatching — abstract sink + a deduplicating manager.

Alerts are notifications about *events* (an order filled, a signal rejected by
the risk gate, an LLM failure, an execution error). They are never used to make
trading decisions — the deterministic risk engine always runs first.

The manager is dependency-free (no network). The :class:`AlertSink` protocol is
the seam for a concrete delivery channel (Telegram, email, webhooks); tests
inject a fake sink.
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


class TelegramAlertSink:
    """Deliver alerts via the Telegram Bot API (``sendMessage``).

    Uses the already-present ``httpx`` dependency — no extra packages. Failures
    are logged and reported as ``False`` so a dead bot never breaks the cycle.
    """

    def __init__(self, bot_token: str, chat_id: str, timeout_seconds: float = 10.0) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    async def send(self, event: str, message: str, severity: str) -> bool:
        if not self.configured:
            logger.warning("telegram sink not configured; dropping alert", extra={"event": event})
            return False
        import httpx

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, json={"chat_id": self._chat_id, "text": f"[{severity}] {event}: {message}"}
                )
                resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram send failed", extra={"event": event, "error": str(exc)})
            return False


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
