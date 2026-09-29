"""Stock earnings dates via yfinance (§7.18; stocks agent — tested with mocks only).

``yfinance.Ticker(symbol).get_earnings_dates(limit=…)`` returns a DataFrame indexed
by tz-aware timestamps (past and upcoming). Each date within the look-ahead (plus
recent ones, so the after-window still applies) becomes one ``earnings`` event.
yfinance is blocking, so every lookup runs in a worker thread; one symbol's failure
never drops the others.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from ...core.models import EventImportance, EventKind, MarketEvent
from .base import ContextBatch

logger = structlog.get_logger()

SOURCE = "yfinance"


def _default_ticker_factory(symbol: str) -> Any:
    import yfinance  # optional "stocks" extra — imported only when earnings are on

    return yfinance.Ticker(symbol)


class EarningsProvider:
    name = SOURCE

    def __init__(
        self,
        lookahead_days: int,
        ticker_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._lookahead = timedelta(days=lookahead_days)
        self._ticker = ticker_factory or _default_ticker_factory

    def _dates(self, symbol: str) -> list[datetime]:
        frame = self._ticker(symbol).get_earnings_dates(limit=8)
        if frame is None:
            return []
        dates: list[datetime] = []
        for stamp in getattr(frame, "index", []):
            value = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
            if not isinstance(value, datetime):
                continue
            dates.append(value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC))
        return dates

    async def fetch(self, symbols: list[str], now: datetime) -> ContextBatch:
        events: list[MarketEvent] = []
        stocks = [s for s in symbols if "/" not in s]  # crypto pairs have no earnings
        failures = 0
        for symbol in stocks:
            try:
                dates = await asyncio.to_thread(self._dates, symbol)
            except Exception as exc:  # noqa: BLE001 - one ticker never sinks the pass
                failures += 1
                logger.warning("earnings lookup failed", symbol=symbol, error=str(exc))
                continue
            for at in dates:
                if now - timedelta(days=7) <= at <= now + self._lookahead:
                    events.append(
                        MarketEvent(
                            source=SOURCE,
                            kind=EventKind.EARNINGS,
                            at=at,
                            title=f"{symbol.upper()} earnings",
                            asset=symbol.upper(),
                            importance=EventImportance.HIGH,
                        )
                    )
        if stocks and failures == len(stocks):
            raise ValueError("earnings lookup failed for every symbol")
        # A rescheduled report must not keep blocking at its old date — but only a
        # complete pass may re-sync (a failed symbol would lose its stored dates).
        window = (now - timedelta(days=7), now + self._lookahead) if failures == 0 else None
        return ContextBatch(source=SOURCE, events=events, sync_window=window)
