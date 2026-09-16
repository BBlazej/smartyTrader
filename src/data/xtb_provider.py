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
from datetime import UTC, datetime, timedelta
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

    async def fetch_history(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[OHLCV]:
        """Historical candles in ``[start, end]`` (UTC) for the backtester (§7.14).

        Delegates to an optional ``fetch_history`` on the injected source; sources
        without range support raise — the backtester needs arbitrary windows, which
        ``fetch_snapshot``'s trailing-N contract cannot provide.
        """
        fetch_history = getattr(self._source, "fetch_history", None)
        if fetch_history is None:
            raise TypeError(
                f"data source {type(self._source).__name__} does not support historical ranges"
            )
        raw = await fetch_history(symbol, timeframe, start, end) or []
        return [self._to_candle(row) for row in raw]

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

    # timeframe → (yfinance period, yfinance interval).
    # Daily bars must be deep enough for MACD (needs ≥ 26 closes + signal window):
    # the old "1mo" request ≈ 21 closes silently left the stocks prompt without
    # MACD while crypto (100 candles) had it (§7.11). "6mo" ≈ 126 trading days.
    _PERIOD_MAP: ClassVar[dict[str, tuple[str, str]]] = {
        "1h": ("1d", "1h"),
        "1d": ("6mo", "1d"),
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

    async def fetch_history(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[list[Any]]:
        """Historical rows in ``[start, end]`` (UTC) for the backtester (§7.14).

        yfinance's ``end`` argument is *exclusive*, so we pad a day to keep the
        requested last day inside the window; the rows are then filtered strictly.
        """
        _, yf_interval = self._PERIOD_MAP.get(interval, ("3mo", "1d"))
        start_iso = start.astimezone(UTC).date().isoformat()
        end_exclusive_iso = (end.astimezone(UTC).date() + timedelta(days=1)).isoformat()
        df = await asyncio.to_thread(
            self._fetch_range, symbol, start_iso, end_exclusive_iso, yf_interval
        )
        rows = self._to_rows(df, limit=10**9)
        start_ms = int(start.astimezone(UTC).timestamp() * 1000)
        end_ms = int(end.astimezone(UTC).timestamp() * 1000)
        return [row for row in rows if row[0] is not None and start_ms <= row[0] <= end_ms]

    @staticmethod
    def _fetch(symbol: str, period: str, interval: str) -> Any:
        import yfinance as yf  # lazy import

        return yf.Ticker(symbol).history(period=period, interval=interval)

    @staticmethod
    def _fetch_range(symbol: str, start_iso: str, end_iso: str, interval: str) -> Any:
        import yfinance as yf  # lazy import

        return yf.Ticker(symbol).history(start=start_iso, end=end_iso, interval=interval)

    @staticmethod
    def _to_rows(df: Any, limit: int) -> list[list[Any]]:
        """Flatten a yfinance-style frame (``iterrows()`` → (ts, row)) into row lists.

        Rows with a NaN OHLC value are **dropped**: the old NaN→0.0 coercion
        turned a missing Low into a zero-low candle that poisoned ATR and the
        Bollinger bands downstream (§7.11). Volume is not indicator-critical, so
        a NaN volume stays coerced to 0.0.

        Duck-typed so it works on a real pandas DataFrame *or* any object exposing
        ``iterrows()`` — keeping the provider decoupled from pandas in tests.
        """
        if df is None:
            return []
        rows: list[list[Any]] = []
        dropped = 0
        for ts, row in df.iterrows():
            ohlc = (_f(row["Open"]), _f(row["High"]), _f(row["Low"]), _f(row["Close"]))
            if any(math.isnan(v) for v in ohlc):
                dropped += 1
                continue
            rows.append(
                [
                    int(ts.timestamp() * 1000),
                    ohlc[0],
                    ohlc[1],
                    ohlc[2],
                    ohlc[3],
                    _nan_to_zero(_f(row["Volume"])),
                ]
            )
        if dropped:
            logger.warning(
                "dropped candles with NaN OHLC values (would poison indicators as fake zeros)",
                dropped=dropped,
            )
        return rows[-limit:]


def _nan_to_zero(value: float) -> float:
    """Map a NaN to 0.0 — safe only for non-indicator-critical cells (volume)."""
    return 0.0 if math.isnan(value) else value


def _f(value: Any) -> float:
    """Coerce a numeric cell to a float; NaN is *preserved* for the caller.

    The old blanket NaN→0.0 coercion silently manufactured zero-priced candles
    (§7.11); callers now decide — OHLC NaNs drop the row, volume NaNs become 0.0.
    """
    return float(value)


def create_xtb_provider(candles_limit: int = 100) -> XTBProvider:
    """Build a provider backed by a yfinance data source.

    The ``yfinance`` import here is *eager on purpose*: this factory is
    yfinance-specific, so a missing dependency surfaces at build time (where the
    runner turns it into an actionable install hint) instead of re-raising as a
    fetch error every cycle. :class:`XTBProvider` itself stays decoupled — inject
    any :class:`StockDataSource` to run without yfinance.
    """
    import yfinance  # noqa: F401 — eager availability check, used by YFinanceSource

    return XTBProvider(YFinanceSource(), candles_limit=candles_limit)
