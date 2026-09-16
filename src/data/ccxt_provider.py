"""Crypto market data provider via CCXT (Kraken)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from ..core.models import OHLCV, MarketSnapshot

logger = structlog.get_logger()


class ExchangeClient(Protocol):
    """Minimal async CCXT surface the provider depends on."""

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[list[Any]]: ...


class CCXTProvider:
    """Fetches OHLCV candles from a CCXT exchange and wraps them in a MarketSnapshot.

    The exchange client is injected so the provider is testable without a network
    connection or a real CCXT instance.
    """

    def __init__(self, client: ExchangeClient, candles_limit: int = 100) -> None:
        self._client = client
        self._candles_limit = candles_limit
        self._closed = False

    @property
    def client(self) -> ExchangeClient:
        """Expose the underlying exchange (useful for sharing with an executor)."""
        return self._client

    async def close(self) -> None:
        """Release the underlying exchange, closing its ``aiohttp`` session.

        CCXT's async exchange owns a lazily-created ``aiohttp.ClientSession``
        that must be closed explicitly, otherwise it leaks on shutdown
        (``Unclosed client session`` + the exchange's own ``.close()`` warning).
        Idempotent and fail-soft: a client without a ``close`` (e.g. a test
        stand-in) is a no-op, and a failing close never aborts shutdown.
        """
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("exchange close failed (continuing shutdown)", error=str(exc))

    async def fetch_snapshot(self, symbol: str, timeframe: str = "1h") -> MarketSnapshot:
        """Fetch candles for ``symbol`` at ``timeframe`` and return a MarketSnapshot."""
        logger.debug("fetching snapshot", symbol=symbol, timeframe=timeframe)
        raw = await self._client.fetch_ohlcv(symbol, timeframe, limit=self._candles_limit)
        if raw is None:
            raw = []
        candles = [self._to_candle(row) for row in raw]
        return MarketSnapshot(symbol=symbol, timeframe=timeframe, candles=candles)

    async def fetch_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        page_size: int = 500,
        max_pages: int = 40,
    ) -> list[OHLCV]:
        """Fetch historical candles in ``[start, end]`` (UTC), paginating CCXT (§7.14).

        The backtester needs arbitrary date ranges — ``fetch_snapshot`` only returns
        the most recent ``candles_limit`` bars, and the agent does not run 24/7 so
        stored snapshots are too sparse to replay from. Pages advance by the last
        returned timestamp; a page that makes no progress stops the loop (a venue
        quirk must never spin). Results are de-duplicated and sorted oldest-first.
        """
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        by_ts: dict[int, OHLCV] = {}
        since_ms = start_ms
        for _ in range(max_pages):
            rows = await self._client.fetch_ohlcv(
                symbol, timeframe, since=since_ms, limit=page_size
            )
            if not rows:
                break
            for row in rows:
                candle = self._to_candle(row)
                ts = row[0]
                if ts is None or candle.timestamp is None:
                    continue
                if start_ms <= int(ts) <= end_ms:
                    by_ts[int(ts)] = candle
            last_ts = int(rows[-1][0])
            if last_ts >= end_ms or last_ts <= since_ms:
                break  # reached the end, or no forward progress — stop paginating
            since_ms = last_ts + 1
        else:
            logger.warning(
                "history pagination hit its page cap; range may be truncated",
                symbol=symbol,
                max_pages=max_pages,
            )
        return [by_ts[ts] for ts in sorted(by_ts)]

    @staticmethod
    def _to_candle(row: list[Any]) -> OHLCV:
        """Convert a CCXT OHLCV row ``[ts_ms, o, h, l, c, v]`` into a model."""
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


def create_ccxt_provider(
    exchange_id: str = "kraken",
    testnet: bool = True,
    api_key: str | None = None,
    api_secret: str | None = None,
    candles_limit: int = 100,
) -> CCXTProvider:
    """Build a provider backed by a real CCXT exchange.

    CCXT is imported lazily so the rest of the package (and its tests) stays
    importable without the dependency installed.
    """
    import ccxt.async_support as ccxt_async  # lazy import

    exchange_cls = getattr(ccxt_async, exchange_id)
    params: dict[str, Any] = {"enableRateLimit": True}
    if api_key:
        params["apiKey"] = api_key
    if api_secret:
        params["secret"] = api_secret

    client = exchange_cls(params)
    if testnet:
        client.set_sandbox_mode(True)

    return CCXTProvider(client, candles_limit=candles_limit)
