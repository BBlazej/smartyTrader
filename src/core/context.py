"""Market context: the refresh job and the per-decision reader (§7.18, CHANGE.md P5).

* :class:`ContextRefresher` — a fail-soft background job (like the §7.70 watchlist
  refresh): runs every enabled provider, isolates each one's failure and persists
  its batch (calendar feeds re-sync their window, everything else is insert-only).
  It never touches the trade path; a dead feed only means older context.
* :class:`ContextReader` — what the decision pipeline asks per symbol: fresh
  sentiment, scheduled events around ``now`` (macro for the configured economies,
  this asset's earnings), this asset's delisting notices and the latest unexpired
  context card. Pure reads; the pipeline renders them into the prompt and the risk
  engine's event guard checks them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog

from ..data.context.base import ContextProvider, base_asset
from .config import ContextSettings
from .models import EventKind, SymbolContext

logger = structlog.get_logger()

#: Past events the reader still returns — covers every guard's after-window
#: (``event_blackout_after_minutes``, ``earnings_blackout_hours_after``).
EVENT_LOOKBACK = timedelta(days=2)
#: How far back delisting notices stay visible (the guard applies its own days).
NOTICE_LOOKBACK = timedelta(days=365)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ContextRefresher:
    """Runs the providers and persists what they return (fail-soft per provider)."""

    def __init__(self, *, storage: object, providers: Sequence[ContextProvider], component: str):
        self._storage = storage
        self._providers = list(providers)
        self._component = component

    @property
    def providers(self) -> list[ContextProvider]:
        return list(self._providers)

    async def refresh(self, symbols: list[str], now: datetime | None = None) -> dict[str, str]:
        """One pass over every provider; returns ``{provider: "ok (…)" | "failed: …"}``."""
        moment = _aware(now or datetime.now(UTC))
        status: dict[str, str] = {}
        for provider in self._providers:
            try:
                batch = await provider.fetch(list(symbols), moment)
            except Exception as exc:  # noqa: BLE001 - one source never blocks the others
                status[provider.name] = f"failed: {exc}"
                logger.warning(
                    "context source failed; keeping stored data",
                    component=self._component,
                    source=provider.name,
                    error=str(exc),
                )
                continue
            try:
                stored = await self._persist(batch)
            except Exception as exc:  # noqa: BLE001
                status[provider.name] = f"failed: storage: {exc}"
                logger.warning(
                    "context persist failed",
                    component=self._component,
                    source=provider.name,
                    error=str(exc),
                )
                continue
            status[provider.name] = "ok (" + ", ".join(f"{k} {v}" for k, v in stored.items()) + ")"
        logger.info("market context refreshed", component=self._component, sources=status)
        return status

    async def _persist(self, batch) -> dict[str, int]:
        storage = self._storage
        stored: dict[str, int] = {}
        if batch.sync_window is not None:
            start, end = batch.sync_window
            inserted, deleted = await storage.sync_market_events(  # type: ignore[attr-defined]
                batch.source, batch.events, start=start, end=end
            )
            stored["events"] = inserted
            if deleted:
                stored["events_removed"] = deleted
        elif batch.events:
            stored["events"] = await storage.store_market_events(batch.events)  # type: ignore[attr-defined]
        if batch.sentiment:
            stored["sentiment"] = await storage.store_sentiment(batch.sentiment)  # type: ignore[attr-defined]
        if batch.news:
            stored["news"] = await storage.store_news_items(batch.news)  # type: ignore[attr-defined]
        return stored


class ContextReader:
    """Builds the :class:`SymbolContext` a decision sees (reads only)."""

    def __init__(self, storage: object, settings: ContextSettings) -> None:
        self._storage = storage
        self._settings = settings

    async def for_symbol(self, symbol: str, now: datetime) -> SymbolContext:
        """Raises on a storage error — the pipeline then blocks entries (fail-closed)."""
        moment = _aware(now)
        asset = base_asset(symbol)
        settings = self._settings
        storage = self._storage

        events = await storage.get_market_events(  # type: ignore[attr-defined]
            moment - EVENT_LOOKBACK,
            moment + timedelta(hours=max(settings.lookahead_hours, 48.0)),
            assets=[asset],
            kinds=[EventKind.MACRO, EventKind.EARNINGS],
        )
        notices = await storage.get_market_events(  # type: ignore[attr-defined]
            moment - NOTICE_LOOKBACK,
            moment + timedelta(days=1),
            assets=[asset],
            kinds=[EventKind.DELISTING],
        )
        notices = [n for n in notices if n.asset == asset]

        sentiment = None
        if settings.sentiment.enabled:
            reading = await storage.get_latest_sentiment(settings.sentiment.source)  # type: ignore[attr-defined]
            if reading is not None and moment - _aware(reading.as_of) <= timedelta(
                hours=settings.sentiment.max_age_hours
            ):
                sentiment = reading

        card = None
        if settings.summarizer.enabled:
            card = await storage.get_active_context_card(symbol, now=moment)  # type: ignore[attr-defined]

        return SymbolContext(
            symbol=symbol,
            now=moment,
            lookahead_hours=settings.lookahead_hours,
            sentiment=sentiment,
            events=events,
            notices=notices,
            card=card,
        )
