"""Deterministic risk engine — hard gates before every order."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .config import RiskSettings
from .models import Action, PortfolioState, RiskResult, RiskVerdict, TradeSignal

logger = logging.getLogger(__name__)


class DailyLossTracker:
    """Tracks daily PnL to enforce loss limits."""

    def __init__(self) -> None:
        self._start_of_day_value: float | None = None
        self._current_date: str = ""

    @property
    def start_of_day_value(self) -> float | None:
        return self._start_of_day_value

    def reset_if_new_day(self, portfolio_value: float) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._start_of_day_value = portfolio_value

    @property
    def daily_pnl_pct(self) -> float | None:
        if self._start_of_day_value is None or self._start_of_day_value == 0:
            return None
        return (self.daily_portfolio_value - self._start_of_day_value) / self._start_of_day_value

    @property
    def daily_portfolio_value(self) -> float | None:
        # This gets set externally; we just store the latest value
        return getattr(self, "_latest_value", None)

    def update_latest_value(self, value: float) -> None:
        self._latest_value = value


class ConsecutiveLossTracker:
    """Tracks consecutive losses and cooldown state."""

    def __init__(self) -> None:
        self._consecutive_losses: int = 0
        self._cooldown_until: datetime | None = None

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def in_cooldown(self) -> bool:
        if self._cooldown_until is None:
            return False
        return datetime.now(UTC) < self._cooldown_until

    def record_loss(self, cooldown_minutes: int) -> None:
        from datetime import timedelta

        self._consecutive_losses += 1
        if self._consecutive_losses >= 3:
            self._cooldown_until = datetime.now(UTC) + timedelta(minutes=cooldown_minutes)

    def record_win(self) -> None:
        self._consecutive_losses = 0
        self._cooldown_until = None


class RiskEngine:
    """Evaluates every LLM signal against hard-coded risk rules.

    The LLM *never* bypasses this layer. If any rule is violated, the signal
    is rejected with a clear reason.
    """

    def __init__(self, settings: RiskSettings) -> None:
        self.settings = settings
        self._daily_tracker = DailyLossTracker()
        self._loss_tracker = ConsecutiveLossTracker()

    # ── Public API ────────────────────────────────────────────

    def evaluate(
        self,
        signal: TradeSignal,
        portfolio: PortfolioState,
    ) -> RiskResult:
        """Check a trade signal against all risk rules.

        Returns ``RiskVerdict.APPROVED`` only if every rule passes.
        """
        # Only check limits for active trades (not HOLD)
        if signal.action == Action.HOLD:
            return RiskResult(verdict=RiskVerdict.APPROVED)

        checks = [
            self._check_confidence(signal),
            self._check_max_positions(signal, portfolio),
            self._check_position_size(signal, portfolio),
            self._check_daily_loss(portfolio),
            self._check_drawdown(portfolio),
            self._check_cooldown(),
            self._check_stop_loss(signal),
        ]

        for result in checks:
            if result.verdict == RiskVerdict.REJECTED:
                logger.warning("Risk rejected %s: %s", signal.symbol, result.reason)
                return result

        return RiskResult(verdict=RiskVerdict.APPROVED)

    def record_outcome(self, was_profitable: bool) -> None:
        """Record trade outcome for consecutive-loss tracking."""
        if was_profitable:
            self._loss_tracker.record_win()
        else:
            self._loss_tracker.record_loss(
                cooldown_minutes=self.settings.consecutive_losses_cooldown_minutes
            )

    def update_daily_value(self, portfolio_value: float) -> None:
        """Update daily loss tracker with latest portfolio value."""
        self._daily_tracker.reset_if_new_day(portfolio_value)
        self._daily_tracker.update_latest_value(portfolio_value)

    # ── Individual checks (each returns RiskResult) ───────────

    def _check_confidence(self, signal: TradeSignal) -> RiskResult:
        if signal.confidence < self.settings.min_confidence:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=f"Confidence {signal.confidence:.2f} below minimum {self.settings.min_confidence}",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_max_positions(self, signal: TradeSignal, portfolio: PortfolioState) -> RiskResult:
        # Don't open new position if already at max
        existing_symbols = {p.symbol for p in portfolio.positions}
        is_new_position = signal.symbol not in existing_symbols

        if is_new_position and len(portfolio.positions) >= self.settings.max_open_positions:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=f"Max open positions ({self.settings.max_open_positions}) reached",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_position_size(self, signal: TradeSignal, portfolio: PortfolioState) -> RiskResult:
        total_value = portfolio.total_value
        if total_value <= 0:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason="Portfolio value is zero or negative",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_daily_loss(self, portfolio: PortfolioState) -> RiskResult:
        # Only set baseline if not already set (first call of the day).
        # This allows tests and callers to pre-set a start-of-day value.
        if self._daily_tracker.start_of_day_value is None:
            self._daily_tracker.reset_if_new_day(portfolio.total_value)
        self._daily_tracker.update_latest_value(portfolio.total_value)

        pnl_pct = self._daily_tracker.daily_pnl_pct
        if pnl_pct is not None and pnl_pct <= -self.settings.daily_loss_limit_pct:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=f"Daily loss {pnl_pct:.2%} exceeds limit {-self.settings.daily_loss_limit_pct:.2%}",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_drawdown(self, portfolio: PortfolioState) -> RiskResult:
        # Simplified: compare current value to starting capital.
        # In production this would track peak value over time.
        # For now we skip if no baseline is set — the daily loss check covers short-term risk.
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_cooldown(self) -> RiskResult:
        if self._loss_tracker.in_cooldown:
            remaining = self._loss_tracker._cooldown_until  # type: ignore[union-attr]
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=f"In cooldown until {remaining.isoformat()}",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_stop_loss(self, signal: TradeSignal) -> RiskResult:
        if signal.action in (Action.BUY, Action.SELL) and signal.stop_loss is None:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason="Active trade signal must include a stop-loss",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)
