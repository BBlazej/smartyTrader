"""Alert dispatching — abstract sink + a deduplicating manager.

Alerts are notifications about *events* (an order filled, a signal rejected by
the risk gate, an LLM outage, an execution error). They are never used to make
trading decisions — the deterministic risk engine always runs first.

The :class:`AlertSink` protocol is the seam for delivery channels: the log sink is
always on (audit trail), and :class:`WebhookAlertSink` (§7.51) delivers to a real
channel — Slack/Discord/generic JSON webhooks or ntfy — when ``ALERT_WEBHOOK_URL``
is set. Tests inject fake sinks / an httpx mock transport.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable

import httpx
import structlog

# structlog like the rest of the codebase (AGENTS.md logging rule) — alerts are
# exactly the lines that must land in the configured renderers.
logger = structlog.get_logger()

#: Severity order for the webhook's minimum-severity filter.
SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "error": 2}


@runtime_checkable
class AlertSink(Protocol):
    """A channel that delivers a single alert payload."""

    async def send(self, event: str, message: str, severity: str) -> bool: ...


class NoopAlertSink:
    """Log-only sink: every alert becomes a structured ``alert`` log line."""

    async def send(self, event: str, message: str, severity: str) -> bool:
        logger.info("alert", alert_event=event, severity=severity, text=message)
        return True


class WebhookAlertSink:
    """POST alerts to a webhook (§7.51) — a real channel beyond the log.

    ``fmt="json"`` sends ``{"text", "content", "event", "severity", "message"}`` —
    ``text`` is what Slack (and most generic receivers) render, ``content`` what
    Discord renders. ``fmt="ntfy"`` sends the plain message with ntfy's ``Title`` /
    ``Priority`` / ``Tags`` headers. Alerts below ``min_severity`` are skipped
    (counted as handled). Delivery failures return ``False``; they never raise.
    The URL is a secret (webhook tokens live in it) — it comes from the
    environment, never from ``settings.yaml`` or the dashboard.
    """

    def __init__(
        self,
        url: str,
        *,
        fmt: str = "json",
        min_severity: str = "warning",
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if fmt not in ("json", "ntfy"):
            raise ValueError("alert webhook format must be 'json' or 'ntfy'")
        self._url = url
        self._fmt = fmt
        self._min_rank = SEVERITY_RANK.get(min_severity, SEVERITY_RANK["warning"])
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def send(self, event: str, message: str, severity: str) -> bool:
        if SEVERITY_RANK.get(severity, 0) < self._min_rank:
            return True
        text = f"[{severity}] {event}: {message}"
        try:
            if self._fmt == "ntfy":
                resp = await self._client.post(
                    self._url,
                    content=message.encode(),
                    headers={
                        "Title": f"trading-agent {event}",
                        "Priority": "high" if severity == "error" else "default",
                        "Tags": severity,
                    },
                )
            else:
                resp = await self._client.post(
                    self._url,
                    json={
                        "text": text,
                        "content": text,
                        "event": event,
                        "severity": severity,
                        "message": message,
                    },
                )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - a dead channel must not break trading
            logger.warning("alert webhook delivery failed", alert_event=event, error=str(exc))
            return False
        return True

    async def close(self) -> None:
        await self._client.aclose()


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
                logger.debug("alert deduplicated", alert_event=event, symbol=symbol)
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
                    alert_event=event,
                    sink=type(sink).__name__,
                    error=str(exc),
                )
        if not delivered:
            logger.error("all alert sinks failed", alert_event=event, symbol=symbol)
        return delivered

    def reset_dedup(self) -> None:
        """Clear the dedup state (useful between cycles/tests)."""
        self._last_sent.clear()

    async def close(self) -> None:
        """Release sinks that hold connections (webhook HTTP clients)."""
        for sink in self._sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                    logger.debug("alert sink close failed", error=str(exc))
