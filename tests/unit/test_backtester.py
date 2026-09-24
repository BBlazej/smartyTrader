"""Tests for the decision-replay backtester (§7.14) — synthetic data, exact numbers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.backtester import (
    DecisionReplayBacktester,
    ReplayDecision,
    max_drawdown,
    sharpe_ratio,
)
from src.core.config import RiskSettings
from src.core.models import OHLCV


def _ts(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


def _candles(closes: list[float], start_day: int = 1) -> list[OHLCV]:
    return [
        OHLCV(
            timestamp=_ts(start_day + i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1.0,
        )
        for i, c in enumerate(closes)
    ]


def _settings(**overrides: object) -> RiskSettings:
    base = {
        "max_position_pct": 0.10,
        "daily_loss_limit_pct": 0.02,
        "max_drawdown_pct": 0.05,
        "consecutive_losses_cooldown_minutes": 60,
        "max_open_positions": 5,
        "min_confidence": 0.6,
        "enforce_exit_levels": True,
    }
    base.update(overrides)
    return RiskSettings(**base)


def _backtester(**kwargs: object) -> DecisionReplayBacktester:
    return DecisionReplayBacktester(
        risk_settings=_settings(),
        initial_cash=float(kwargs.pop("initial_cash", 10_000.0)),
        fee_pct=float(kwargs.pop("fee_pct", 0.0)),
        slippage_pct=float(kwargs.pop("slippage_pct", 0.0)),
    )


class TestMetrics:
    def test_max_drawdown_known_curve(self) -> None:
        # peak 120 → trough 60 ⇒ 50%
        assert max_drawdown([100.0, 120.0, 60.0, 80.0]) == pytest.approx(0.5)

    def test_max_drawdown_monotonic_up_is_zero(self) -> None:
        assert max_drawdown([1.0, 2.0, 3.0]) == 0.0

    def test_sharpe_positive_for_steady_gain(self) -> None:
        values = [100.0, 101.0, 102.0, 103.0]
        assert sharpe_ratio(values, 365.0) > 0

    def test_sharpe_zero_when_no_dispersion_or_too_few_points(self) -> None:
        assert sharpe_ratio([100.0, 100.0, 100.0], 365.0) == 0.0
        assert sharpe_ratio([100.0, 101.0], 365.0) == 0.0


class TestReplayEngine:
    async def test_buy_then_sell_books_exact_pnl(self) -> None:
        candles = {"X": _candles([100.0, 110.0, 120.0])}
        decisions = [
            ReplayDecision(_ts(1, 12), "X", "buy", 0.8, stop_loss=90.0),
            ReplayDecision(_ts(3, 12), "X", "sell", 0.7),
        ]
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        # Buy: 10% of 10k / 100 = 10 units. The later SELL closes the whole position,
        # exactly like live (§7.47 — it used to sell a cap-sized 8.5-unit slice).
        assert report.per_symbol["X"]["trades"] == 1
        assert report.per_symbol["X"]["realized_pnl"] == pytest.approx(200.0)  # 10 × (120−100)
        assert report.final_equity == pytest.approx(10_200.0)
        assert report.total_return_pct == pytest.approx(2.0)
        assert report.win_rate == pytest.approx(1.0)
        assert report.closed_trades == 1

    async def test_daily_loss_cap_resets_when_replay_day_advances(self) -> None:
        """§7.27: replay runs on market time — tomorrow gets a fresh daily-loss window."""

        def _c(ts_hour_closes: list[tuple[int, int, float]]) -> list[OHLCV]:
            return [
                OHLCV(
                    timestamp=_ts(day, hour),
                    open=c,
                    high=c,
                    low=c,
                    close=c,
                    volume=1.0,
                )
                for day, hour, c in ts_hour_closes
            ]

        candles = {
            "X": _c([(1, 0, 100.0), (1, 12, 80.0), (2, 0, 80.0)]),
        }
        decisions = [
            ReplayDecision(_ts(1, 6), "X", "buy", 0.8, stop_loss=50.0),  # approved
            ReplayDecision(_ts(1, 13), "X", "buy", 0.8, stop_loss=50.0),  # daily cap (-20%)
            ReplayDecision(_ts(2, 9), "X", "buy", 0.8, stop_loss=50.0),  # new day → approved
        ]
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        # Pre-§7.27 the whole replay was one wall-clock "today": buy #3 would also
        # have been rejected by the still-breaching cumulative cap.
        assert report.risk_rejected == 1
        # Buy 1: 10 units @100 (cash 9000). Mark to 80 → equity 9800. Buy 3: 12.25
        # units @80 (cash 8020) → equity back to 9800.
        assert report.final_equity == pytest.approx(9_800.0)

    async def test_stop_loss_auto_exit_without_decision(self) -> None:
        candles = {"X": _candles([100.0, 94.0])}  # day2 close breaches the 95 stop
        decisions = [ReplayDecision(_ts(1, 12), "X", "buy", 0.8, stop_loss=95.0)]
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        assert report.auto_exits == 1
        assert report.per_symbol["X"]["realized_pnl"] == pytest.approx(-60.0)  # 10 × (94−100)
        assert report.win_rate == pytest.approx(0.0)

    async def test_take_profit_auto_exit(self) -> None:
        candles = {"X": _candles([100.0, 108.0])}
        # Stop included: the live risk gate requires one on entries.
        decisions = [ReplayDecision(_ts(1, 12), "X", "buy", 0.8, stop_loss=95.0, take_profit=105.0)]
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        assert report.auto_exits == 1
        assert report.per_symbol["X"]["realized_pnl"] == pytest.approx(80.0)

    async def test_exit_enforcement_disableable(self) -> None:
        candles = {"X": _candles([100.0, 94.0])}
        decisions = [ReplayDecision(_ts(1, 12), "X", "buy", 0.8, stop_loss=95.0)]
        bt = DecisionReplayBacktester(
            risk_settings=_settings(enforce_exit_levels=False), initial_cash=10_000.0
        )
        report = await bt.replay(decisions, candles, timeframe="1d")

        assert report.auto_exits == 0
        assert report.closed_trades == 0  # position stays open; PnL unrealized

    async def test_low_confidence_decision_rejected_by_risk_gate(self) -> None:
        candles = {"X": _candles([100.0, 101.0])}
        decisions = [ReplayDecision(_ts(1, 12), "X", "buy", 0.5)]  # below min_confidence 0.6
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        assert report.risk_rejected == 1
        assert report.closed_trades == 0
        assert report.final_equity == pytest.approx(10_000.0)

    async def test_holds_counted_and_never_trade(self) -> None:
        candles = {"X": _candles([100.0, 101.0])}
        decisions = [ReplayDecision(_ts(1, 12), "X", "hold", 0.9)]
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        assert report.holds == 1
        assert report.closed_trades == 0

    async def test_fees_and_slippage_flow_into_pnl(self) -> None:
        # Fee 1%/side, no slippage. Buy: 10@100 + 10 fee → cash 8990.
        # The SELL closes all 10 units (§7.47): sell fee 12, the whole buy fee 10
        # ⇒ net PnL = 200 − 10 − 12 = 178.
        candles = {"X": _candles([100.0, 120.0])}
        decisions = [
            ReplayDecision(_ts(1, 12), "X", "buy", 0.8, stop_loss=90.0),
            ReplayDecision(_ts(2, 12), "X", "sell", 0.7),
        ]
        report = await _backtester(fee_pct=0.01).replay(decisions, candles, timeframe="1d")

        qty = 10.0
        expected_pnl = qty * 20.0 - 10.0 - qty * 120.0 * 0.01
        assert report.per_symbol["X"]["realized_pnl"] == pytest.approx(expected_pnl, abs=1e-3)
        # Equity moves only by the sell-side fee at the mark (sold at current price).
        assert report.final_equity == pytest.approx(10_190.0 - qty * 120.0 * 0.01, abs=1e-3)

    async def test_buy_and_hold_benchmark(self) -> None:
        candles = {"X": _candles([100.0, 150.0])}
        report = await _backtester().replay([], candles, timeframe="1d")

        assert report.buy_and_hold_pct["X"] == pytest.approx(50.0)
        assert report.blended_buy_and_hold_pct == pytest.approx(50.0)
        assert report.total_return_pct == pytest.approx(0.0)  # no decisions → no trades

    async def test_multi_symbol_shared_book(self) -> None:
        candles = {
            "A": _candles([100.0, 120.0]),
            "B": _candles([50.0, 40.0]),
        }
        decisions = [
            ReplayDecision(_ts(1, 12), "A", "buy", 0.8, stop_loss=90.0),
            ReplayDecision(_ts(1, 13), "B", "buy", 0.8, stop_loss=45.0),
            # B's day-2 close (40) breaches its 45 stop → auto exit at −10/unit… qty:
            # total value at that mark ≈ 10k; 10% of it / 50 = 2 units? No: qty sized at
            # decision time when B is unmarked: notional cap uses last close 50 → 20 units.
        ]
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        # A closed by nothing (stays open); B auto-exited on its stop.
        assert report.auto_exits == 1
        assert report.per_symbol["B"]["losses"] == 1
        assert "A" not in report.per_symbol  # never closed → no realized stats

    async def test_decision_without_price_is_skipped(self) -> None:
        # Decision predates every candle for that symbol → unpriceable, skipped.
        candles = {"X": _candles([100.0], start_day=5)}
        decisions = [ReplayDecision(_ts(1), "X", "buy", 0.9, stop_loss=90.0)]
        report = await _backtester().replay(decisions, candles, timeframe="1d")

        assert report.risk_rejected == 1
        assert report.closed_trades == 0


class TestReplayDeterminism:
    async def test_same_inputs_same_report(self) -> None:
        candles = {"X": _candles([100.0, 95.0, 110.0])}
        decisions = [
            ReplayDecision(_ts(1, 12), "X", "buy", 0.8, stop_loss=94.0, take_profit=109.0),
        ]
        first = await _backtester().replay(decisions, candles, timeframe="1d")
        second = await _backtester().replay(decisions, candles, timeframe="1d")

        assert first.to_dict() == second.to_dict()  # deterministic: zero LLM calls (§7.14)
