"""Stocks market-data provider — OHLCV candles wrapped in a ``MarketSnapshot``.

Mirrors :class:`src.data.ccxt_provider.CCXTProvider`: the data source is injected
behind a :class:`StockDataSource` protocol so the provider is testable without a
network connection or a real xAPI / yfinance instance.

Data source
-----------
The default source (built by :func:`create_xtb_provider`) is backed by **yfinance**
for historical OHLCV candles. Real-time quotes from XTB's **xAPI** are a documented
extension point: implement :class:`StockDataSource` against the xAPI REST surface and
inject it to layer a live bid/ask onto the last candle. xAPI access is the external
blocker tracked in ``PLAN.md`` (OAuth2 + an approved demo account).
"""

from __future__ import annotations

import asyncio
import inspect
import math
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

import structlog

from ..core.models import OHLCV, MarketSnapshot

logger = structlog.get_logger()


class StockDataSource(Protocol):
    """Minimal async surface the provider depends on.

    A source yields OHLCV rows shaped like ``[ts_ms, open, high, low, close, volume]``
    (oldest → newest). This keeps the provider decoupled from yfinance / xAPI.
    """

    async def fetch_ohlcv(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[list[Any]]: ...


class XTBProvider:
    """Fetches OHLCV candles from a :class:`StockDataSource` and wraps them in a ``MarketSnapshot``.

    The data source is injected so the provider is testable without a network
    connection or a real yfinance / xAPI instance.
    """

    def __init__(self, source: StockDataSource, candles_limit: int = 100) -> None:
        self._source = source
        self._candles_limit = candles_limit

    @property
    def source(self) -> StockDataSource:
        """Expose the underlying data source (useful for sharing / debugging)."""
        return self._source

    async def close(self) -> None:
        """Release the underlying data source.

        The default yfinance source is synchronous and holds no persistent
        session, so this is a no-op today; it exists to keep the provider
        the interface uniform with :class:`CCXTProvider` (which *does* own an
        ``aiohttp`` session) so runners can close any provider generically.
        """
        close = getattr(self._source, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("data source close failed (continuing shutdown)", error=str(exc))

    async def fetch_snapshot(self, symbol: str, timeframe: str = "1d") -> MarketSnapshot:
        """Fetch candles for ``symbol`` at ``timeframe`` and return a MarketSnapshot."""
        logger.debug("fetching snapshot", symbol=symbol, timeframe=timeframe)
        raw = await self._source.fetch_ohlcv(symbol, timeframe, limit=self._candles_limit)
        if raw is None:
            raw = []
        candles = [self._to_candle(row) for row in raw]
        return MarketSnapshot(symbol=symbol, timeframe=timeframe, candles=candles)

    @staticmethod
    def _to_candle(row: list[Any]) -> OHLCV:
        """Convert a stock OHLCV row ``[ts_ms, o, h, l, c, v]`` into a model."""
        ts_ms = row[0]
        timestamp = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC) if ts_ms else None
        return OHLCV(
            timestamp=timestamp,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )


# ── yfinance-backed source (default) ──────────────────────────


class YFinanceSource:
    """Yields historical OHLCV rows via yfinance (lazy import, thread offload).

    yfinance is a synchronous library; the call is pushed to a worker thread so the
    async event loop is never blocked. The import is lazy so the rest of the package
    (and its tests) stays importable without the dependency installed.
    """

    # timeframe → (yfinance period, yfinance interval)
    _PERIOD_MAP: ClassVar[dict[str, tuple[str, str]]] = {
        "1h": ("1d", "1h"),
        "1d": ("1mo", "1d"),
        "1w": ("1y", "1wk"),
    }

    async def fetch_ohlcv(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[list[Any]]:
        period, yf_interval = self._PERIOD_MAP.get(interval, ("3mo", "1d"))
        df = await asyncio.to_thread(self._fetch, symbol, period, yf_interval)
        return self._to_rows(df, limit)

    @staticmethod
    def _fetch(symbol: str, period: str, interval: str) -> Any:
        import yfinance as yf  # lazy import

        return yf.Ticker(symbol).history(period=period, interval=interval)

    @staticmethod
    def _to_rows(df: Any, limit: int) -> list[list[Any]]:
        """Flatten a yfinance-style frame (``iterrows()`` → (ts, row)) into row lists.

        Duck-typed so it works on a real pandas DataFrame *or* any object exposing
        ``iterrows()`` — keeping the provider decoupled from pandas in tests.
        """
        if df is None:
            return []
        rows: list[list[Any]] = []
        for ts, row in df.iterrows():
            rows.append(
                [
                    int(ts.timestamp() * 1000),
                    _f(row["Open"]),
                    _f(row["High"]),
                    _f(row["Low"]),
                    _f(row["Close"]),
                    _f(row["Volume"]),
                ]
            )
        return rows[-limit:]


def _f(value: Any) -> float:
    """Coerce a (possibly NaN) numeric cell to a float, mapping NaN → 0.0."""
    result = float(value)
    return 0.0 if math.isnan(result) else result


def create_xtb_provider(candles_limit: int = 100) -> XTBProvider:
    """Build a provider backed by a yfinance data source.

    yfinance is imported lazily inside :class:`YFinanceSource`, so this factory only
    fails (at fetch time, not import time) if the dependency is missing.
    """
    return XTBProvider(YFinanceSource(), candles_limit=candles_limit)
