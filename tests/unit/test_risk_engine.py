"""Tests for the deterministic risk engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


class TestDailyLossEpsilon:
    """§7.37 (find #3): boundary comparisons are epsilon-tolerant, biased to reject."""

    def test_boundary_within_epsilon_rejects(self, risk_settings: RiskSettings) -> None:
        engine = RiskEngine(risk_settings)
        # Baseline exactly 10k; the mark sits a hair *above* the -2% line so raw
        # float math lands just short of it — rounding must not buy an approval.
        engine.restore_daily_baseline(10_000.0)
        portfolio = PortfolioState(cash=9_800.0000001, positions=[])
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="r",
            stop_loss=1.0,
        )
        result = engine.evaluate(signal, portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "daily loss" in (result.reason or "").lower()

    def test_clearly_inside_limit_still_approves(self, risk_settings: RiskSettings) -> None:
        engine = RiskEngine(risk_settings)
        engine.restore_daily_baseline(10_000.0)
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="r",
            stop_loss=1.0,
        )
        result = engine.evaluate(signal, PortfolioState(cash=9_850.0, positions=[]))
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

    def test_closing_sell_needs_no_stop(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        # §7.9: a close reduces exposure; requiring a stop on it only blocked
        # legitimate exits (and stranded losing positions during cooldowns).
        signal = TradeSignal(
            symbol="AAPL",  # held in healthy_portfolio
            action=Action.SELL,
            confidence=0.9,
            reasoning="exit the trade",
            stop_loss=None,
        )

        result = engine.evaluate(signal, healthy_portfolio)
        assert result.verdict == RiskVerdict.APPROVED


class TestEntryGeometry:
    """§7.54: entries need sane levels — stop_loss < price < take_profit, and the
    stop may not sit absurdly below price (unbounded per-trade risk)."""

    @staticmethod
    def _buy(stop_loss: float | None = 95.0, take_profit: float | None = None) -> TradeSignal:
        return TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="entry",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def test_stop_above_or_at_price_is_rejected(
        self, engine: RiskEngine, healthy_portfolio
    ) -> None:
        for bad_stop in (105.0, 100.0):
            result = engine.evaluate(
                self._buy(stop_loss=bad_stop), healthy_portfolio, current_price=100.0
            )
            assert result.verdict == RiskVerdict.REJECTED, bad_stop
            assert "not below the current price" in (result.reason or "")

    def test_take_profit_at_or_below_price_is_rejected(
        self, engine: RiskEngine, healthy_portfolio
    ) -> None:
        for bad_tp in (100.0, 98.0):
            result = engine.evaluate(
                self._buy(stop_loss=95.0, take_profit=bad_tp),
                healthy_portfolio,
                current_price=100.0,
            )
            assert result.verdict == RiskVerdict.REJECTED, bad_tp
            assert "not above the current price" in (result.reason or "")

    def test_stop_further_than_max_distance_is_rejected(
        self, engine: RiskEngine, healthy_portfolio
    ) -> None:
        # Default max_stop_distance_pct = 25%: an SL at 0.01 is unbounded risk.
        result = engine.evaluate(self._buy(stop_loss=0.01), healthy_portfolio, current_price=100.0)
        assert result.verdict == RiskVerdict.REJECTED
        assert "max_stop_distance_pct" in (result.reason or "")

    def test_boundary_distance_and_sane_levels_approved(
        self, engine: RiskEngine, healthy_portfolio
    ) -> None:
        sane = engine.evaluate(
            self._buy(stop_loss=75.0, take_profit=110.0), healthy_portfolio, current_price=100.0
        )
        assert sane.verdict == RiskVerdict.APPROVED  # exactly 25% under price → allowed

    def test_geometry_only_applies_when_the_mark_is_known(
        self, engine: RiskEngine, healthy_portfolio
    ) -> None:
        # Legacy callers without current_price keep the old stop-required behavior.
        result = engine.evaluate(self._buy(stop_loss=105.0), healthy_portfolio)
        assert result.verdict == RiskVerdict.APPROVED


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


class TestDrawdown:
    """The max-drawdown rule is live [§7.5]: equity below ``max_drawdown_pct``
    of its high-water mark blocks active signals."""

    @staticmethod
    def _active_signal() -> TradeSignal:
        return TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="Recovery attempt",
            stop_loss=59000.0,
        )

    def test_rejects_beyond_max_drawdown(self, engine: RiskEngine) -> None:
        engine.seed_peak_equity(10_000.0)
        portfolio = PortfolioState(cash=9_400.0, positions=[])  # -6% from peak

        result = engine.evaluate(self._active_signal(), portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "drawdown" in (result.reason or "").lower()

    def test_approves_within_max_drawdown(self, engine: RiskEngine) -> None:
        engine.seed_peak_equity(10_000.0)
        portfolio = PortfolioState(cash=9_700.0, positions=[])  # -3% from peak

        result = engine.evaluate(self._active_signal(), portfolio)
        assert result.verdict == RiskVerdict.APPROVED

    def test_first_reading_seeds_peak_lazily(self, engine: RiskEngine) -> None:
        """No history at all ⇒ the first valuation defines the peak."""
        portfolio = PortfolioState(cash=10_000.0, positions=[])
        assert engine.peak_equity is None
        result = engine.evaluate(self._active_signal(), portfolio)
        assert result.verdict == RiskVerdict.APPROVED
        assert engine.peak_equity == pytest.approx(10_000.0)

    def test_seed_never_lowers_the_peak(self, engine: RiskEngine) -> None:
        engine.seed_peak_equity(10_000.0)
        engine.seed_peak_equity(5_000.0)
        assert engine.peak_equity == pytest.approx(10_000.0)

    def test_new_highs_raise_the_peak(self, engine: RiskEngine) -> None:
        engine.update_daily_value(10_000.0)
        engine.update_daily_value(11_000.0)
        assert engine.peak_equity == pytest.approx(11_000.0)


class TestPositionSizeGate:
    """The gate caps the *planned* order notional at max_position_pct [§7.5]."""

    @staticmethod
    def _active_signal() -> TradeSignal:
        return TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="Full size attempt",
            stop_loss=59000.0,
        )

    def test_rejects_oversized_planned_notional(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        # total_value = 10_000 + 10 × 155 = 11_550 → cap = 1_155
        result = engine.evaluate(self._active_signal(), healthy_portfolio, planned_notional=2_000.0)
        assert result.verdict == RiskVerdict.REJECTED
        assert "position" in (result.reason or "").lower()

    def test_approves_notional_at_cap(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        result = engine.evaluate(self._active_signal(), healthy_portfolio, planned_notional=1_155.0)
        assert result.verdict == RiskVerdict.APPROVED

    def test_no_plan_still_passes_size_rule(
        self, engine: RiskEngine, healthy_portfolio: PortfolioState
    ) -> None:
        # Callers without pipeline sizing (e.g. tests of other rules) keep the
        # previous behavior: only the zero-value guard applies.
        result = engine.evaluate(self._active_signal(), healthy_portfolio)
        assert result.verdict == RiskVerdict.APPROVED


class TestPerPositionCap:
    """§7.42: max_position_pct caps the *position*, not each order."""

    @staticmethod
    def _buy(symbol: str = "BTC/USDT") -> TradeSignal:
        return TradeSignal(
            symbol=symbol, action=Action.BUY, confidence=0.9, reasoning="add", stop_loss=1.0
        )

    @staticmethod
    def _book(btc_value: float, cash: float = 9_000.0) -> PortfolioState:
        return PortfolioState(
            cash=cash,
            positions=[
                Position(
                    symbol="BTC/USDT",
                    quantity=btc_value / 100.0,
                    avg_entry_price=100.0,
                    current_price=100.0,
                )
            ],
        )

    def test_rejects_buy_when_position_already_at_cap(self, engine: RiskEngine) -> None:
        # total 10_000 → cap 1_000, already holding 1_000.
        result = engine.evaluate(self._buy(), self._book(1_000.0), planned_notional=10.0)
        assert result.verdict == RiskVerdict.REJECTED
        assert "already at max size" in (result.reason or "")

    def test_rejects_add_that_would_overshoot(self, engine: RiskEngine) -> None:
        result = engine.evaluate(self._buy(), self._book(600.0), planned_notional=500.0)
        assert result.verdict == RiskVerdict.REJECTED
        assert "already holding 600.00" in (result.reason or "")

    def test_approves_add_within_headroom(self, engine: RiskEngine) -> None:
        # total 9_600 → cap 960; 600 held + 360 planned fits exactly.
        result = engine.evaluate(self._buy(), self._book(600.0), planned_notional=360.0)
        assert result.verdict == RiskVerdict.APPROVED

    def test_other_symbols_and_sells_are_unaffected(self, engine: RiskEngine) -> None:
        book = self._book(1_000.0)
        assert engine.evaluate(self._buy("ETH/USDT"), book, planned_notional=900.0).verdict == (
            RiskVerdict.APPROVED
        )
        sell = TradeSignal(symbol="BTC/USDT", action=Action.SELL, confidence=0.9, reasoning="x")
        assert engine.evaluate(sell, book, planned_notional=500.0).verdict == RiskVerdict.APPROVED


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

    def test_daily_value_is_none_before_first_update(self) -> None:
        # Declared attribute, honest ``float | None`` — no getattr hack (§7.19).
        tracker = DailyLossTracker()
        assert tracker.daily_portfolio_value is None
        assert tracker.daily_pnl_pct is None


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

    def test_cooldown_until_public_accessor(self) -> None:
        # The rejection reason reads this instead of poking _cooldown_until
        # with a type: ignore (§7.19).
        tracker = ConsecutiveLossTracker()
        assert tracker.cooldown_until is None
        for _ in range(3):
            tracker.record_loss(cooldown_minutes=60)
        assert tracker.cooldown_until is not None
        assert tracker.in_cooldown

    def test_threshold_is_configurable(self, risk_settings: RiskSettings) -> None:
        risk_settings.consecutive_losses_threshold = 2
        engine = RiskEngine(risk_settings)
        portfolio = PortfolioState(cash=10_000.0, positions=[])
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="go",
            stop_loss=59_000.0,
        )

        engine.record_outcome(was_profitable=False)
        assert engine.evaluate(signal, portfolio).verdict == RiskVerdict.APPROVED

        engine.record_outcome(was_profitable=False)  # streak of 2 == configured threshold
        result = engine.evaluate(signal, portfolio)
        assert result.verdict == RiskVerdict.REJECTED
        assert "cooldown" in result.reason.lower()


class TestClockInjection:
    """§7.27: stateful trackers evaluate against an injected clock."""

    class FakeClock:
        def __init__(self, start: datetime) -> None:
            self.current = start

        def now(self) -> datetime:
            return self.current

    def test_daily_tracker_rolls_day_on_injected_clock(self) -> None:
        clock = self.FakeClock(datetime(2026, 1, 1, 23, 0, tzinfo=UTC))
        tracker = DailyLossTracker(clock)
        tracker.reset_if_new_day(10_000.0)
        tracker.update_latest_value(9_000.0)
        assert tracker.daily_pnl_pct == pytest.approx(-0.1)

        # Same injected day → baseline kept; crossing midnight on the fake clock
        # (not wall-clock) re-baselines.
        clock.current += timedelta(hours=2)
        tracker.reset_if_new_day(9_000.0)
        assert tracker.start_of_day_value == pytest.approx(9_000.0)

    def test_cooldown_expires_per_injected_clock(self) -> None:
        clock = self.FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        tracker = ConsecutiveLossTracker(clock)
        tracker.record_loss(cooldown_minutes=30, threshold=1)
        assert tracker.in_cooldown

        clock.current += timedelta(minutes=29)
        assert tracker.in_cooldown
        clock.current += timedelta(minutes=2)
        assert not tracker.in_cooldown

    def test_engine_defaults_to_system_clock(self, risk_settings: RiskSettings) -> None:
        # No injected clock → live behavior (real now), cooldown active immediately.
        engine = RiskEngine(risk_settings)
        engine.record_outcome(was_profitable=False)
        for _ in range(3):
            engine.record_outcome(was_profitable=False)
        assert engine._loss_tracker.in_cooldown


class TestDailyRolloverAtCheck:
    """§7.59 L3: the first risk check after UTC midnight uses *today's* baseline."""

    class FakeClock:
        def __init__(self, start: datetime) -> None:
            self.current = start

        def now(self) -> datetime:
            return self.current

    def test_first_check_of_new_day_rebaselines(self, risk_settings: RiskSettings) -> None:
        clock = self.FakeClock(datetime(2026, 1, 1, 23, 0, tzinfo=UTC))
        engine = RiskEngine(risk_settings, clock=clock)
        signal = TradeSignal(
            symbol="BTC/USDT", action=Action.BUY, confidence=0.9, reasoning="x", stop_loss=90.0
        )
        assert engine.evaluate(signal, PortfolioState(cash=1_000.0)).verdict == RiskVerdict.APPROVED

        # Overnight the book fell 3 % (> the 2 % daily limit) — that loss belongs to
        # yesterday. No post-processing ran in between (the rollover used to live
        # only there), yet today's first check must start a fresh baseline.
        clock.current += timedelta(hours=2)
        result = engine.evaluate(signal, PortfolioState(cash=970.0))
        assert result.verdict == RiskVerdict.APPROVED, result.reason

        # A further 3 % drop *within* the new day is still caught.
        result = engine.evaluate(signal, PortfolioState(cash=940.0))
        assert result.verdict == RiskVerdict.REJECTED
        assert "Daily loss" in (result.reason or "")

    def test_restored_baseline_survives_the_check(self, risk_settings: RiskSettings) -> None:
        clock = self.FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
        engine = RiskEngine(risk_settings, clock=clock)
        engine.restore_daily_baseline(1_000.0)  # restart rehydration (§7.7)
        signal = TradeSignal(
            symbol="BTC/USDT", action=Action.BUY, confidence=0.9, reasoning="x", stop_loss=90.0
        )
        result = engine.evaluate(signal, PortfolioState(cash=970.0))
        assert result.verdict == RiskVerdict.REJECTED
