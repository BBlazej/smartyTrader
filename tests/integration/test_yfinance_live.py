"""Live yfinance smoke test (§7.63) — the ONLY tests that hit the real network.

Everything else exercises the stocks provider through the injected-source seam;
this module proves the real `YFinanceSource` path works end to end: the §7.11
"6mo" daily depth, the §7.67 "1mo" hourly depth, NaN-row dropping and the
backtester's range fetch against Yahoo's servers.

Opt-in and excluded from default runs (`pytest -m "not network"` is in the
project `addopts`); run on a connected machine with:

    pytest tests/integration/test_yfinance_live.py -m network --no-cov -q

"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.network]


@pytest.fixture(scope="module")
def provider():
    from src.data.xtb_provider import create_xtb_provider

    p = create_xtb_provider()
    yield p
    # close is sync/no-op for yfinance but keep the protocol honest.
    import asyncio

    asyncio.run(p.close())


class TestLiveDailySnapshot:
    async def test_aapl_daily_book_is_deep_and_clean(self, provider) -> None:
        snapshot = await provider.fetch_snapshot("AAPL", "1d")
        assert snapshot.symbol == "AAPL"
        # §7.11: "6mo" must give enough closes for MACD (≥26), with margin.
        assert len(snapshot.candles) >= 60
        for candle in snapshot.candles[-40:]:
            assert candle.open > 0 and candle.high > 0
            assert candle.low > 0 and candle.close > 0
            assert candle.low <= candle.high
            # NaN would fail the comparisons above; state it explicitly too.
            assert candle.close == candle.close

    async def test_indicators_compute_on_the_live_book(self, provider) -> None:
        from src.analysis.indicators import compute_indicators

        snapshot = await provider.fetch_snapshot("AAPL", "1d")
        indicators = compute_indicators(snapshot.candles)
        for key in ("rsi_14", "macd_line", "macd_signal", "bb_upper", "atr_14"):
            assert key in indicators, f"{key} missing on a live {len(snapshot.candles)}-candle book"


class TestLiveHourlyDepth:
    async def test_hourly_book_feeds_macd(self, provider) -> None:
        """§7.67: the old "1d" period fetched ~7 hourly bars; "1mo" must not."""
        snapshot = await provider.fetch_snapshot("AAPL", "1h")
        assert len(snapshot.candles) >= 26


class TestLiveHistoryRange:
    async def test_fetch_history_respects_window(self, provider) -> None:
        end = datetime.now(UTC) - timedelta(days=1)
        start = end - timedelta(days=14)
        candles = await provider.fetch_history("AAPL", "1d", start, end)
        assert len(candles) >= 5  # ~10 trading days expected
        first_ts, last_ts = candles[0].timestamp, candles[-1].timestamp
        assert first_ts is not None and last_ts is not None
        assert start <= first_ts and last_ts <= end + timedelta(days=1)
