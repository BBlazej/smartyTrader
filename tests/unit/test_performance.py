"""Performance ledger + baselines (§7.73, CHANGE.md §4.2 / P2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analysis.baselines import (
    baseline_returns,
    best_baseline,
    buy_and_hold_return,
    equal_weight,
    ma_crossover_return,
)
from src.core.backtester import DecisionReplayBacktester
from src.core.config import RiskSettings
from src.core.models import OHLCV
from src.core.performance import max_drawdown_fraction, sleeve_performance
from src.core.storage import Storage

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _candles(closes: list[float], step: timedelta = timedelta(hours=1)) -> list[OHLCV]:
    return [
        OHLCV(timestamp=T0 + i * step, open=c, high=c, low=c, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


class TestBaselines:
    def test_buy_and_hold_is_net_of_both_sides(self) -> None:
        assert buy_and_hold_return(_candles([100, 120])) == pytest.approx(0.20)
        assert buy_and_hold_return(_candles([100, 120]), cost_pct=0.01) == pytest.approx(
            1.2 * 0.99**2 - 1
        )
        assert buy_and_hold_return(_candles([100])) is None

    def test_crossover_has_no_look_ahead(self) -> None:
        # Flat for 60 bars, then one jump on the very last bar: the crossover decides
        # on closes it has seen (fast == slow → not above → stays in cash).
        closes = [100.0] * 60 + [200.0]
        assert ma_crossover_return(_candles(closes)) == pytest.approx(0.0)
        assert buy_and_hold_return(_candles(closes)) == pytest.approx(1.0)

    def test_crossover_rides_a_trend_after_the_signal(self) -> None:
        closes = [100.0 * 1.01**i for i in range(120)]
        ma = ma_crossover_return(_candles(closes), cost_pct=0.001)
        hold = buy_and_hold_return(_candles(closes), cost_pct=0.001)
        assert ma is not None and hold is not None
        assert 0 < ma < hold  # in the trend, but only once the averages crossed

    def test_crossover_needs_depth_and_valid_windows(self) -> None:
        assert ma_crossover_return(_candles([100.0] * 50)) is None
        with pytest.raises(ValueError, match="fast < slow"):
            ma_crossover_return(_candles([100.0] * 60), fast=50, slow=20)

    def test_blend_and_best(self) -> None:
        assert equal_weight([0.1, None, 0.3]) == pytest.approx(0.2)
        assert equal_weight([None]) is None
        baselines = baseline_returns({"A": _candles([100, 110]), "B": _candles([100, 90])})
        assert baselines["buy_and_hold"] == pytest.approx(0.0)
        assert baselines["ma_crossover_20_50"] is None and baselines["cash"] == 0.0
        assert best_baseline(baselines) == ("buy_and_hold", pytest.approx(0.0))
        assert best_baseline({"x": None}) is None

    async def test_backtest_report_compares_with_baselines(self) -> None:
        backtester = DecisionReplayBacktester(
            risk_settings=RiskSettings(), initial_cash=10_000, fee_pct=0.001
        )
        report = await backtester.replay([], {"A": _candles([100, 105, 110])}, timeframe="1h")
        assert report.baselines_pct["buy_and_hold"] == pytest.approx((1.1 * 0.999**2 - 1) * 100)
        assert report.best_baseline == "buy_and_hold"
        assert report.beats_best_baseline is False  # doing nothing loses to holding


def _order(side: str, qty: float, hours: float, pnl: float | None = None, symbol: str = "BTC/EUR"):
    return SimpleNamespace(
        side=side, quantity=qty, symbol=symbol, filled_at=T0 + timedelta(hours=hours),
        realized_pnl=pnl,
    )  # fmt: skip


class TestSleevePerformance:
    def test_trades_ratios_and_holding_time(self) -> None:
        orders = [
            _order("buy", 2, 0),
            _order("sell", 1, 2, pnl=10.0),
            _order("sell", 1, 6, pnl=-4.0),
            _order("buy", 1, 10, symbol="ETH/EUR"),  # still open — not a trade
        ]
        perf = sleeve_performance("swing", orders, [100.0, 110.0, 99.0, 105.0])
        assert (perf.closed_trades, perf.wins, perf.losses) == (2, 1, 1)
        assert perf.realized_pnl == pytest.approx(6.0)
        assert perf.win_rate == pytest.approx(0.5)
        assert perf.profit_factor == pytest.approx(2.5)
        assert (perf.avg_win, perf.avg_loss) == (10.0, -4.0)
        assert perf.avg_holding_hours == pytest.approx(4.0)
        assert perf.max_drawdown_pct == pytest.approx(0.1)
        assert perf.to_dict()["strategy"] == "swing"

    def test_empty_sleeve(self) -> None:
        perf = sleeve_performance("position", [], [])
        assert perf.closed_trades == 0 and perf.win_rate is None
        assert perf.profit_factor is None and perf.avg_holding_hours is None
        assert perf.max_drawdown_pct is None
        assert max_drawdown_fraction([100.0, 120.0]) == 0.0


class TestLedgerStorage:
    async def test_strategy_orders_and_equity_series(self, tmp_path: Path) -> None:
        store = Storage(str(tmp_path / "perf.db"), agent="crypto")
        await store.initialize()
        store.bind_venue("paper")
        try:
            await store.save_order("b", "BTC/EUR", "buy", 1, 100, "filled", filled_at=T0,
                                   strategy="swing")  # fmt: skip
            await store.save_order("p", "BTC/EUR", "buy", 1, 100, "pending", strategy="swing")
            await store.save_order("o", "BTC/EUR", "buy", 1, 100, "filled", filled_at=T0,
                                   strategy="position")  # fmt: skip
            await store.save_order("s", "BTC/EUR", "sell", 1, 110, "filled",
                                   filled_at=T0 + timedelta(hours=3), realized_pnl=10.0,
                                   strategy="swing")  # fmt: skip
            orders = await store.get_strategy_orders("swing")
            assert [o.order_id for o in orders] == ["b", "s"]
            later = await store.get_strategy_orders("swing", since=T0 + timedelta(hours=1))
            assert [o.order_id for o in later] == ["s"]
            await store.save_sleeve_snapshot("swing", equity=100.0, cash=100.0)
            await store.save_sleeve_snapshot("swing", equity=90.0, cash=90.0)
            await store.save_sleeve_snapshot("position", equity=1.0, cash=1.0)
            assert await store.get_sleeve_equity_series("swing") == [100.0, 90.0]
        finally:
            await store.close()


class TestBacktestSleeveCli:
    def test_sleeve_spec_lookup(self) -> None:
        from scripts.backtest import _sleeve_spec
        from src.core.config import Settings

        settings = Settings()
        assert _sleeve_spec(settings, "crypto", None) is None
        spec = _sleeve_spec(settings, "crypto", "crypto_position")
        assert spec is not None and spec.timeframe == "4h" and spec.weight == 0.5
        with pytest.raises(SystemExit, match="not a configured sleeve"):
            _sleeve_spec(settings, "crypto", "nope")
