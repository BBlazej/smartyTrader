"""Unit tests for the CCXT market-data provider."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import OHLCV, MarketSnapshot
from src.data.ccxt_provider import CCXTProvider


@pytest.fixture()
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def provider(mock_client: AsyncMock) -> CCXTProvider:
    return CCXTProvider(mock_client, candles_limit=50)


class TestFetchSnapshot:
    @pytest.mark.asyncio
    async def test_returns_snapshot_with_candles(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_ohlcv.return_value = [
            [1700000000000, 100.0, 102.0, 99.0, 101.0, 500.0],
            [1700000060000, 101.0, 103.0, 100.0, 102.0, 600.0],
        ]

        snapshot = await provider.fetch_snapshot("BTC/USDT", "1h")

        assert isinstance(snapshot, MarketSnapshot)
        assert snapshot.symbol == "BTC/USDT"
        assert snapshot.timeframe == "1h"
        assert len(snapshot.candles) == 2
        assert all(isinstance(c, OHLCV) for c in snapshot.candles)

    @pytest.mark.asyncio
    async def test_passes_candles_limit(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_ohlcv.return_value = []
        await provider.fetch_snapshot("ETH/USDT", "4h")
        mock_client.fetch_ohlcv.assert_awaited_once_with("ETH/USDT", "4h", limit=50)

    @pytest.mark.asyncio
    async def test_none_response_returns_empty_candles(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_ohlcv.return_value = None
        snapshot = await provider.fetch_snapshot("BTC/USDT", "1d")
        assert snapshot.candles == []

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_candles(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_ohlcv.return_value = []
        snapshot = await provider.fetch_snapshot("BTC/USDT", "1h")
        assert snapshot.candles == []


class TestFetchHistory:
    """Paginated range fetch for the backtester (§7.14)."""

    @staticmethod
    def _row(day: int, close: float = 100.0) -> list:
        ts = int(datetime(2026, 1, day, tzinfo=UTC).timestamp() * 1000)
        return [ts, 100.0, 101.0, 99.0, close, 10.0]

    @pytest.mark.asyncio
    async def test_paginates_until_end(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 3, tzinfo=UTC)
        mock_client.fetch_ohlcv.side_effect = [
            [self._row(1), self._row(2)],
            [self._row(3)],
        ]

        candles = await provider.fetch_history("BTC/USDT", "1d", start, end)

        assert [c.timestamp.day for c in candles] == [1, 2, 3]
        calls = mock_client.fetch_ohlcv.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["since"] == int(start.timestamp() * 1000)
        assert calls[1].kwargs["since"] > calls[0].kwargs["since"]

    @pytest.mark.asyncio
    async def test_rows_beyond_window_are_filtered(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 3, tzinfo=UTC)
        # Venue overshoots the window on its only page.
        mock_client.fetch_ohlcv.side_effect = [[self._row(2), self._row(5)]]

        candles = await provider.fetch_history("BTC/USDT", "1d", start, end)

        assert [c.timestamp.day for c in candles] == [2]

    @pytest.mark.asyncio
    async def test_empty_first_page_returns_nothing(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_ohlcv.side_effect = [[]]
        candles = await provider.fetch_history(
            "BTC/USDT", "1d", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
        )
        assert candles == []

    @pytest.mark.asyncio
    async def test_no_progress_page_stops_loop(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        # A venue quirk returning the same timestamp must never spin the loop.
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 3, tzinfo=UTC)
        stuck_page = [self._row(2)]
        mock_client.fetch_ohlcv.side_effect = lambda *a, **k: stuck_page

        candles = await provider.fetch_history("BTC/USDT", "1d", start, end, max_pages=50)

        assert [c.timestamp.day for c in candles] == [2]
        # first page advances past day 2; the repeat makes no progress → stop
        assert mock_client.fetch_ohlcv.call_count == 2

    @pytest.mark.asyncio
    async def test_overlapping_pages_are_deduplicated(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 5, tzinfo=UTC)
        mock_client.fetch_ohlcv.side_effect = [
            [self._row(2), self._row(3)],
            [self._row(3), self._row(4)],
            [],
        ]

        candles = await provider.fetch_history("BTC/USDT", "1d", start, end)

        assert [c.timestamp.day for c in candles] == [2, 3, 4]


class TestToCandle:
    def test_converts_row(self) -> None:
        ts_ms = 1700000000000
        row = [ts_ms, 100.0, 105.0, 95.0, 103.0, 1000.0]
        candle = CCXTProvider._to_candle(row)

        assert candle.timestamp == datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        assert candle.open == 100.0
        assert candle.high == 105.0
        assert candle.low == 95.0
        assert candle.close == 103.0
        assert candle.volume == 1000.0

    def test_none_timestamp(self) -> None:
        row = [None, 1.0, 2.0, 0.5, 1.5, 10.0]
        candle = CCXTProvider._to_candle(row)
        assert candle.timestamp is None

    def test_numeric_coercion(self) -> None:
        # CCXT may return string numbers in some adapters
        row = [1700000000000, "100.5", "101.0", "99.0", "100.75", "1000"]
        candle = CCXTProvider._to_candle(row)
        assert candle.open == 100.5
        assert candle.close == 100.75
        assert candle.volume == 1000.0


class TestClientAccess:
    def test_client_property(self, provider: CCXTProvider, mock_client: AsyncMock) -> None:
        assert provider.client is mock_client


class TestClose:
    """close() must release the underlying exchange's aiohttp session.

    This is the fix for the ``Unclosed client session`` warning on shutdown —
    CCXT's async exchange owns a lazily-created session that needs an explicit
    ``await exchange.close()``.
    """

    @pytest.mark.asyncio
    async def test_closes_exchange_client(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        await provider.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(
        self, provider: CCXTProvider, mock_client: AsyncMock
    ) -> None:
        await provider.close()
        await provider.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_without_close_method_is_noop(self) -> None:
        # A stand-in client with no close (e.g. a bare mock) must not raise.
        provider = CCXTProvider(AsyncMock(spec=["fetch_ohlcv"]), candles_limit=10)
        await provider.close()  # must not raise

    @pytest.mark.asyncio
    async def test_close_failure_does_not_raise(self, provider: CCXTProvider) -> None:
        def _boom() -> None:
            raise RuntimeError("boom")

        provider._client.close = _boom
        # A failing close must never abort shutdown.
        await provider.close()

    @pytest.mark.asyncio
    async def test_close_sync_client(self) -> None:
        # A synchronous close() (not a coroutine) must be handled too.
        provider = CCXTProvider(MagicMock(), candles_limit=10)
        provider._client.close = MagicMock(return_value=None)
        await provider.close()  # must not raise
        provider._client.close.assert_called_once()


class TestSandboxDetection:
    """§7.41: Kraken spot has no ccxt sandbox — say so instead of a TypeError."""

    def test_kraken_spot_has_no_sandbox_but_futures_does(self) -> None:
        from src.data.ccxt_provider import exchange_has_sandbox

        assert exchange_has_sandbox("kraken") is False
        assert exchange_has_sandbox("krakenfutures") is True

    def test_requesting_a_missing_sandbox_is_an_actionable_error(self) -> None:
        from src.data.ccxt_provider import create_ccxt_provider

        with pytest.raises(ValueError, match="no sandbox"):
            create_ccxt_provider(exchange_id="kraken", testnet=True)
