"""Scheduled macro events (§7.18, option (c)): the YAML list + the ForexFactory feed.

* :class:`ConfigMacroProvider` — the operator-maintained ``macro_calendar.events``;
  always available, the reliable base.
* :class:`ForexFactoryProvider` — ``nfs.faireconomy.media/ff_calendar_thisweek.json``,
  an unofficial but widely used weekly feed (title, currency, ISO time with offset,
  impact). Adds what the YAML list misses; when it is down, the YAML list still
  guards.

Both re-publish a window each pass (``sync_window``) so a moved event does not
keep blocking at its old time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from ...core.models import EventImportance, EventKind, MarketEvent
from .base import ContextBatch, safe_label

logger = structlog.get_logger()

CONFIG_SOURCE = "config"
FOREXFACTORY_SOURCE = "forexfactory"

_FF_IMPACT = {
    "high": EventImportance.HIGH,
    "medium": EventImportance.MEDIUM,
    "low": EventImportance.LOW,
}


def _wanted(currency: str, importance: EventImportance, currencies: set[str], floor: int) -> bool:
    return currency in currencies and importance.rank >= floor


class ConfigMacroProvider:
    name = CONFIG_SOURCE

    def __init__(
        self, events: list[dict[str, Any]], currencies: list[str], min_importance: str
    ) -> None:
        self._events = events
        self._currencies = {c.upper() for c in currencies}
        self._floor = EventImportance(min_importance).rank

    async def fetch(self, symbols: list[str], now: datetime) -> ContextBatch:
        events = [
            MarketEvent(
                source=CONFIG_SOURCE,
                kind=EventKind.MACRO,
                at=entry["at"],
                title=entry["title"],
                currency=entry["currency"],
                importance=EventImportance(entry["importance"]),
            )
            for entry in self._events
            if _wanted(
                entry["currency"],
                EventImportance(entry["importance"]),
                self._currencies,
                self._floor,
            )
        ]
        if self._events and all(entry["at"] < now for entry in self._events):
            # The operator list ran out: only the (unofficial) feed guards from here.
            logger.warning(
                "macro_calendar.events has no future entries — extend the list in settings.yaml"
            )
        # Future rows follow the YAML exactly; past ones stay as history.
        return ContextBatch(source=CONFIG_SOURCE, events=events, sync_window=(now, None))


class ForexFactoryProvider:
    name = FOREXFACTORY_SOURCE

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        currencies: list[str],
        min_importance: str,
    ) -> None:
        self._client = client
        self._url = url
        self._currencies = {c.upper() for c in currencies}
        self._floor = EventImportance(min_importance).rank

    async def fetch(self, symbols: list[str], now: datetime) -> ContextBatch:
        response = await self._client.get(self._url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("forexfactory feed: expected a JSON list")  # noqa: TRY004
        events: list[MarketEvent] = []
        times: list[datetime] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            try:
                at = datetime.fromisoformat(str(entry["date"])).astimezone(UTC)
            except (KeyError, ValueError):
                continue
            times.append(at)
            importance = _FF_IMPACT.get(str(entry.get("impact", "")).strip().lower())
            currency = str(entry.get("country", "")).strip().upper()
            title = safe_label(str(entry.get("title", "")), 200)
            if importance is None or not title:
                continue  # "Holiday" / "Non-Economic" rows carry no importance
            if not _wanted(currency, importance, self._currencies, self._floor):
                continue
            events.append(
                MarketEvent(
                    source=FOREXFACTORY_SOURCE,
                    kind=EventKind.MACRO,
                    at=at,
                    title=title,
                    currency=currency,
                    importance=importance,
                )
            )
        if not times:
            # An empty/garbled feed must not wipe the stored week.
            raise ValueError("forexfactory feed: no dated entries")
        # The feed covers one week: re-sync exactly the span it published.
        window = (min(times) - timedelta(minutes=1), max(times) + timedelta(minutes=1))
        return ContextBatch(source=FOREXFACTORY_SOURCE, events=events, sync_window=window)
