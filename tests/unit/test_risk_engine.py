"""Tests for the deterministic risk engine."""

from __future__ import annotations

import pytest

from src.core.config import RiskSettings
from src.core.models import (
    Action,
    PortfolioState,
    Position,
    RiskVerdict,
    TradeSignal,
)
from src.core.risk_engine import ConsecutiveLossTracker, DailyLossTracker, RiskEngine


@pytest.fixture()
def risk_settings() -> RiskSettings:
    return RiskSettings(
        max_position_pct=0.10,
        daily_loss_limit_pct=0.02,
        max_drawdown_pct=0.05,
        consecutive_losses_cooldown_minutes=60,
        max_open_positions=5,
        min_confidence=0.6,
    )


@pytest.fixture()
def engine(risk_settings: RiskSettings) -> RiskEngine:
    return RiskEngine(risk_settings)


@pytest.fixture()
def healthy_portfolio() -> PortfolioState:
    """Portfolio with cash and room for more positions."""
    return PortfolioState(
        cash=10000.0,
        positions=[
            Position(symbol="AAPL", quantity=10, avg_entry_price=150.0, current_price=155.0),
        ],
    )


class TestHOLDAlwaysApproved:
    def test_hold_bypasses_checks(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.HOLD,
            confidence=0.1,  # Below threshold — should still pass for HOLD
            reasoning="Waiting",
        )

        result = engine.evaluate(signal, healthy_portfolio)
        assert result.verdict == RiskVerdict.APPROVED


class TestConfidenceCheck:
    def test_rejects_low_confidence(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.4,  # Below 0.6 threshold
            reasoning="Weak signal",
            stop_loss=59000.0,
        )

        result = engine.evaluate(signal, healthy_portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "confidence" in (result.reason or "").lower()

    def test_approves_high_confidence(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.85,
            reasoning="Strong setup",
            stop_loss=59000.0,
        )

        result = engine.evaluate(signal, healthy_portfolio)
        assert result.verdict == RiskVerdict.APPROVED


class TestMaxPositions:
    def test_rejects_when_at_limit(self, engine: RiskEngine) -> None:
        portfolio = PortfolioState(
            cash=10000.0,
            positions=[
                Position(symbol=f"SYM{i}", quantity=1, avg_entry_price=100.0, current_price=100.0)
                for i in range(5)  # At max of 5
            ],
        )

        signal = TradeSignal(
            symbol="NEW_SYMBOL",  # New position would exceed limit
            action=Action.BUY,
            confidence=0.9,
            reasoning="Good trade",
            stop_loss=90.0,
        )

        result = engine.evaluate(signal, portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "max" in (result.reason or "").lower()

    def test_approves_existing_position(self, engine: RiskEngine) -> None:
        """Adding to an existing position should not count as new."""
        portfolio = PortfolioState(
            cash=10000.0,
            positions=[
                Position(symbol=f"SYM{i}", quantity=1, avg_entry_price=100.0, current_price=100.0)
                for i in range(5)
            ],
        )

        signal = TradeSignal(
            symbol="SYM0",  # Existing position — OK to add
            action=Action.BUY,
            confidence=0.9,
            reasoning="Adding to winner",
            stop_loss=90.0,
        )

        result = engine.evaluate(signal, portfolio)
        assert result.verdict == RiskVerdict.APPROVED


class TestStopLossRequired:
    def test_rejects_without_stop_loss(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="No stop loss provided",
            stop_loss=None,  # Missing!
        )

        result = engine.evaluate(signal, healthy_portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "stop-loss" in (result.reason or "").lower()


class TestDailyLossLimit:
    def test_rejects_on_daily_loss(self, engine: RiskEngine) -> None:
        # Simulate a portfolio that lost >2% today
        engine.update_daily_value(10000.0)  # Start of day
        portfolio = PortfolioState(cash=9800.0, positions=[])  # -2%

        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="Recovery trade",
            stop_loss=59000.0,
        )

        result = engine.evaluate(signal, portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "daily loss" in (result.reason or "").lower()


class TestConsecutiveLossCooldown:
    def test_enters_cooldown_after_3_losses(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        # Record 3 consecutive losses
        for _ in range(3):
            engine.record_outcome(was_profitable=False)

        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="Trying again",
            stop_loss=59000.0,
        )

        result = engine.evaluate(signal, healthy_portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "cooldown" in (result.reason or "").lower()

    def test_resets_on_win(self, engine: RiskEngine, healthy_portfolio: PortfolioState) -> None:
        # 2 losses then a win — should reset counter
        engine.record_outcome(was_profitable=False)
        engine.record_outcome(was_profitable=False)
        engine.record_outcome(was_profitable=True)

        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="Back on track",
            stop_loss=59000.0,
        )

        result = engine.evaluate(signal, healthy_portfolio)
        assert result.verdict == RiskVerdict.APPROVED


class TestZeroPortfolio:
    def test_rejects_zero_value(self, engine: RiskEngine) -> None:
        portfolio = PortfolioState(cash=0.0, positions=[])

        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="All in",
            stop_loss=59000.0,
        )

        result = engine.evaluate(signal, portfolio)
        assert result.verdict == RiskVerdict.REJECTED


class TestDailyLossTracker:
    def test_resets_on_new_day(self) -> None:
        tracker = DailyLossTracker()
        tracker.update_latest_value(10000.0)

        # Should have a value set
        assert tracker.daily_portfolio_value == 10000.0

    def test_pnl_calculation(self) -> None:
        tracker = DailyLossTracker()
        tracker.reset_if_new_day(10000.0)
        tracker.update_latest_value(9800.0)

        assert tracker.daily_pnl_pct == pytest.approx(-0.02)


class TestConsecutiveLossTracker:
    def test_tracks_losses(self) -> None:
        tracker = ConsecutiveLossTracker()
        tracker.record_loss(cooldown_minutes=60)
        tracker.record_loss(cooldown_minutes=60)

        assert tracker.consecutive_losses == 2
        assert not tracker.in_cooldown  # Only triggers at 3+

    def test_cooldown_at_3(self) -> None:
        tracker = ConsecutiveLossTracker()
        for _ in range(3):
            tracker.record_loss(cooldown_minutes=60)

        assert tracker.consecutive_losses == 3
        assert tracker.in_cooldown

    def test_win_resets(self) -> None:
        tracker = ConsecutiveLossTracker()
        for _ in range(5):
            tracker.record_loss(cooldown_minutes=60)

        tracker.record_win()
        assert tracker.consecutive_losses == 0
        assert not tracker.in_cooldown
