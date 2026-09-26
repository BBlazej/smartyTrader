"""Unit tests for the XTB/yfinance market-data provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.core.models import OHLCV, MarketSnapshot
from src.data.xtb_provider import XTBProvider, YFinanceSource


@pytest.fixture()
def mock_source() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def provider(mock_source: AsyncMock) -> XTBProvider:
    return XTBProvider(mock_source, candles_limit=50)


class TestFetchSnapshot:
    @pytest.mark.asyncio
    async def test_returns_snapshot_with_candles(
        self, provider: XTBProvider, mock_source: AsyncMock
    ) -> None:
        mock_source.fetch_ohlcv.return_value = [
            [1700000000000, 100.0, 102.0, 99.0, 101.0, 500.0],
            [1700000060000, 101.0, 103.0, 100.0, 102.0, 600.0],
        ]

        snapshot = await provider.fetch_snapshot("AAPL", "1d")

        assert isinstance(snapshot, MarketSnapshot)
        assert snapshot.symbol == "AAPL"
        assert snapshot.timeframe == "1d"
        assert len(snapshot.candles) == 2
        assert all(isinstance(c, OHLCV) for c in snapshot.candles)

    @pytest.mark.asyncio
    async def test_passes_candles_limit(
        self, provider: XTBProvider, mock_source: AsyncMock
    ) -> None:
        mock_source.fetch_ohlcv.return_value = []
        await provider.fetch_snapshot("MSFT", "1w")
        mock_source.fetch_ohlcv.assert_awaited_once_with("MSFT", "1w", limit=50)

    @pytest.mark.asyncio
    async def test_none_response_returns_empty_candles(
        self, provider: XTBProvider, mock_source: AsyncMock
    ) -> None:
        mock_source.fetch_ohlcv.return_value = None
        snapshot = await provider.fetch_snapshot("AAPL", "1d")
        assert snapshot.candles == []

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_candles(
        self, provider: XTBProvider, mock_source: AsyncMock
    ) -> None:
        mock_source.fetch_ohlcv.return_value = []
        snapshot = await provider.fetch_snapshot("AAPL", "1d")
        assert snapshot.candles == []


class TestToCandle:
    def test_converts_row(self) -> None:
        ts_ms = 1700000000000
        row = [ts_ms, 100.0, 105.0, 95.0, 103.0, 1000.0]
        candle = XTBProvider._to_candle(row)

        assert candle.timestamp == datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        assert candle.open == 100.0
        assert candle.high == 105.0
        assert candle.low == 95.0
        assert candle.close == 103.0
        assert candle.volume == 1000.0

    def test_none_timestamp(self) -> None:
        row = [None, 1.0, 2.0, 0.5, 1.5, 10.0]
        candle = XTBProvider._to_candle(row)
        assert candle.timestamp is None

    def test_numeric_coercion(self) -> None:
        # Sources may return string numbers
        row = [1700000000000, "100.5", "101.0", "99.0", "100.75", "1000"]
        candle = XTBProvider._to_candle(row)
        assert candle.open == 100.5
        assert candle.close == 100.75
        assert candle.volume == 1000.0


class TestSourceAccess:
    def test_source_property(self, provider: XTBProvider, mock_source: AsyncMock) -> None:
        assert provider.source is mock_source


class TestClose:
    """close() keeps the provider interface uniform with :class:`CCXTProvider."""

    @pytest.mark.asyncio
    async def test_closes_source(self, provider: XTBProvider, mock_source: AsyncMock) -> None:
        await provider.close()
        mock_source.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_without_close_method_is_noop(self) -> None:
        provider = XTBProvider(AsyncMock(spec=["fetch_ohlcv"]), candles_limit=10)
        await provider.close()  # must not raise


class TestFetchHistoryRange:
    """Range fetch for the backtester (§7.14)."""

    @pytest.mark.asyncio
    async def test_raises_when_source_lacks_range_support(self) -> None:
        provider = XTBProvider(AsyncMock(spec=["fetch_ohlcv"]))
        with pytest.raises(TypeError, match="does not support historical ranges"):
            await provider.fetch_history(
                "AAPL", "1d", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 5, tzinfo=UTC)
            )

    @pytest.mark.asyncio
    async def test_delegates_to_source_and_converts(self) -> None:
        source = AsyncMock()
        ts_ms = int(datetime(2026, 1, 2, tzinfo=UTC).timestamp() * 1000)
        source.fetch_history.return_value = [[ts_ms, 100.0, 101.0, 99.0, 100.5, 10.0]]
        provider = XTBProvider(source)

        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 5, tzinfo=UTC)
        candles = await provider.fetch_history("AAPL", "1d", start, end)

        assert len(candles) == 1
        assert candles[0].close == 100.5
        source.fetch_history.assert_awaited_once_with("AAPL", "1d", start, end)

    @pytest.mark.asyncio
    async def test_yfinance_source_filters_to_window(self, monkeypatch) -> None:
        frame = _DailyFrame(days=5)  # Jan 1..Jan 5, 2026 midnight UTC
        calls: list[tuple[str, str, str]] = []

        def fake_fetch_range(symbol: str, start_iso: str, end_iso: str, interval: str):
            calls.append((symbol, start_iso, end_iso, interval))
            return frame

        monkeypatch.setattr(YFinanceSource, "_fetch_range", staticmethod(fake_fetch_range))
        source = YFinanceSource()

        rows = await source.fetch_history(
            "AAPL",
            "1d",
            datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 4, 12, tzinfo=UTC),
        )

        # yfinance's `end` is exclusive → padded to Jan 5; rows filtered strictly.
        assert calls == [("AAPL", "2026-01-02", "2026-01-05", "1d")]
        days = [datetime.fromtimestamp(r[0] / 1000, tz=UTC).day for r in rows]
        assert days == [2, 3, 4]


class _DailyFrame:
    """Fake yfinance frame of daily bars starting 2026-01-01 (midnight UTC)."""

    def __init__(self, days: int) -> None:
        self._days = days

    def iterrows(self):
        for i in range(self._days):
            yield (
                datetime(2026, 1, 1 + i, tzinfo=UTC),
                {
                    "Open": 10.0 + i,
                    "High": 11.0 + i,
                    "Low": 9.0 + i,
                    "Close": 10.5 + i,
                    "Volume": 100.0,
                },
            )


class _FakeFrame:
    """Minimal stand-in for a yfinance ``history()`` DataFrame (``iterrows()``)."""

    def __init__(self, n: int) -> None:
        self._n = n

    def __len__(self) -> int:
        return self._n

    def iterrows(self):
        for i in range(self._n):
            ts = datetime.fromtimestamp(1700000000 + i * 3600, tz=UTC)
            row = {
                "Open": 100.0 + i,
                "High": 101.0 + i,
                "Low": 99.0,
                "Close": 100.5 + i,
                "Volume": 1000.0 + i,
            }
            yield ts, row


class TestYFinanceSource:
    def test_to_rows_maps_rows(self) -> None:
        rows = YFinanceSource._to_rows(_FakeFrame(2), limit=10)
        assert len(rows) == 2
        assert rows[0][0] == 1700000000000
        assert rows[0][4] == 100.5
        assert rows[1][1] == 101.0

    def test_to_rows_empty(self) -> None:
        assert YFinanceSource._to_rows(_FakeFrame(0), limit=10) == []

    def test_to_rows_none(self) -> None:
        assert YFinanceSource._to_rows(None, limit=10) == []

    def test_to_rows_truncates_to_limit(self) -> None:
        rows = YFinanceSource._to_rows(_FakeFrame(5), limit=2)
        assert len(rows) == 2


class TestPeriodMap:
    def test_known_timeframes(self) -> None:
        assert YFinanceSource._PERIOD_MAP["1h"] == ("1mo", "1h")
        assert YFinanceSource._PERIOD_MAP["1d"] == ("6mo", "1d")
        assert YFinanceSource._PERIOD_MAP["1w"] == ("1y", "1wk")

    def test_daily_period_deep_enough_for_macd(self) -> None:
        """§7.11: "1mo" ≈ 21 daily closes — below MACD's 26-close minimum."""
        assert YFinanceSource._PERIOD_MAP["1d"][0] == "6mo"

    def test_hourly_period_deep_enough_for_macd(self) -> None:
        """§7.67: "1d" fetched ~7 hourly bars — RSI/MACD/Bollinger silently absent."""
        assert YFinanceSource._PERIOD_MAP["1h"][0] == "1mo"

    def test_unknown_timeframe_defaults(self) -> None:
        period, interval = YFinanceSource._PERIOD_MAP.get("99x", ("3mo", "1d"))
        assert period == "3mo"
        assert interval == "1d"


class _NaNFrame:
    """Fake yfinance frame with a missing-Low row (§7.11 poison case)."""

    def iterrows(self):
        base = datetime(2026, 1, 5, tzinfo=UTC)
        yield base, {"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 100.0}
        yield (
            base + timedelta(days=1),
            {
                "Open": 10.5,
                "High": float("nan"),
                "Low": float("nan"),
                "Close": 10.7,
                "Volume": 200.0,
            },
        )
        yield (
            base + timedelta(days=2),
            {
                "Open": 10.6,
                "High": 11.2,
                "Low": 10.4,
                "Close": 11.0,
                "Volume": float("nan"),
            },
        )


class TestNaNHandling:
    """§7.11: a NaN OHLC row must be dropped, not zero-filled into an ATR/BB poison."""

    def test_nan_ohlc_row_is_dropped(self) -> None:
        rows = YFinanceSource._to_rows(_NaNFrame(), limit=10)
        assert len(rows) == 2
        # survivors keep their real values; no fabricated zeros anywhere
        for row in rows:
            assert all(v > 0 for v in row[1:5])

    def test_nan_volume_becomes_zero(self) -> None:
        rows = YFinanceSource._to_rows(_NaNFrame(), limit=10)
        assert rows[-1][5] == 0.0  # volume NaN → 0.0 (not indicator-critical)

    def test_limit_applies_after_dropping(self) -> None:
        rows = YFinanceSource._to_rows(_NaNFrame(), limit=1)
        assert len(rows) == 1
        assert rows[0][4] == 11.0  # the last *surviving* close


class TestShallowDepthWarning:
    """§7.67: a fetch too shallow to feed MACD must be loud, once."""

    @staticmethod
    def _rows(n: int) -> list[list]:
        base = 1_700_000_000_000
        return [[base + i * 3_600_000, 10.0, 11.0, 9.0, 10.5, 100.0] for i in range(n)]

    async def test_shallow_book_warns_once_per_symbol_timeframe(
        self, mock_source: AsyncMock, provider: XTBProvider
    ) -> None:
        from unittest.mock import patch as _patch

        mock_source.fetch_ohlcv.return_value = self._rows(10)  # < 26 → MACD absent
        with _patch("src.data.xtb_provider.logger") as log:
            await provider.fetch_snapshot("AAPL", "1h")
            await provider.fetch_snapshot("AAPL", "1h")  # deduped

        warns = [
            c for c in log.warning.call_args_list if "depth below MACD minimum" in str(c.args[0])
        ]
        assert len(warns) == 1
        kwargs = warns[0].kwargs
        assert kwargs["symbol"] == "AAPL" and kwargs["timeframe"] == "1h"
        assert kwargs["candles"] == 10

    async def test_deep_book_stays_silent(
        self, mock_source: AsyncMock, provider: XTBProvider
    ) -> None:
        from unittest.mock import patch as _patch

        mock_source.fetch_ohlcv.return_value = self._rows(30)
        with _patch("src.data.xtb_provider.logger") as log:
            await provider.fetch_snapshot("AAPL", "1d")
        assert not [c for c in log.warning.call_args_list if "MACD" in str(c)]
