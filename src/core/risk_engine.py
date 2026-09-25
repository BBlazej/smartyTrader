"""Deterministic risk engine — hard gates before every order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

import structlog

from .config import RiskSettings
from .models import Action, PortfolioState, PositionSide, RiskResult, RiskVerdict, TradeSignal

# structlog like the rest of the codebase — safety-critical rejections must land
# in the configured renderers, not bypass them via stdlib logging (§7.19).
logger = structlog.get_logger()

#: Fallback consecutive-loss streak that triggers the cooldown when no threshold
#: is configured (the shipped default lives in ``RiskSettings``).
DEFAULT_CONSECUTIVE_LOSS_THRESHOLD: Final = 3


def long_exposure(portfolio: PortfolioState, symbol: str) -> float:
    """Market value of the long position already held in ``symbol`` (0 when flat).

    The position-size rule caps *positions*, not single orders (§7.42): a BUY adds to
    this, so the gate and the sizing both start from what is already on the book.
    """
    return sum(
        p.quantity * p.current_price
        for p in portfolio.positions
        if p.symbol == symbol and p.side == PositionSide.LONG
    )


class Clock(Protocol):
    """Source of current time for the stateful risk trackers (§7.27).

    Live trading uses :class:`SystemClock`; the backtester injects a clock that
    follows candle/decision timestamps, so daily-loss windows and cooldowns are
    evaluated against *market* time rather than the wall clock of the replay run.
    """

    def now(self) -> datetime: ...


class SystemClock:
    """Default live clock: ``datetime.now(UTC)``."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class DailyLossTracker:
    """Tracks daily PnL to enforce loss limits."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._start_of_day_value: float | None = None
        self._current_date: str = ""
        self._latest_value: float | None = None

    @property
    def start_of_day_value(self) -> float | None:
        return self._start_of_day_value

    def reset_if_new_day(self, portfolio_value: float) -> None:
        today = self._clock.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._start_of_day_value = portfolio_value

    def restore_today(self, start_of_day_value: float) -> None:
        """Restore today's baseline after a restart (§7.7).

        Without this the -daily-loss rule silently re-baselines to *current*
        value on every restart, forgetting losses already incurred today.
        """
        self._current_date = self._clock.now().strftime("%Y-%m-%d")
        self._start_of_day_value = start_of_day_value

    @property
    def daily_pnl_pct(self) -> float | None:
        if (
            self._start_of_day_value is None
            or self._start_of_day_value == 0
            or self._latest_value is None
        ):
            return None
        return (self._latest_value - self._start_of_day_value) / self._start_of_day_value

    @property
    def daily_portfolio_value(self) -> float | None:
        """Latest portfolio value fed in via :meth:`update_latest_value` (None before any)."""
        return self._latest_value

    def update_latest_value(self, value: float) -> None:
        self._latest_value = value


class ConsecutiveLossTracker:
    """Tracks consecutive losses and cooldown state."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._consecutive_losses: int = 0
        self._cooldown_until: datetime | None = None

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def in_cooldown(self) -> bool:
        if self._cooldown_until is None:
            return False
        return self._clock.now() < self._cooldown_until

    @property
    def cooldown_until(self) -> datetime | None:
        """When the current cooldown expires (None when no cooldown was set)."""
        return self._cooldown_until

    def record_loss(
        self, cooldown_minutes: int, threshold: int = DEFAULT_CONSECUTIVE_LOSS_THRESHOLD
    ) -> None:
        self._consecutive_losses += 1
        if self._consecutive_losses >= threshold:
            self._cooldown_until = self._clock.now() + timedelta(minutes=cooldown_minutes)

    def record_win(self) -> None:
        self._consecutive_losses = 0
        self._cooldown_until = None

    def restore(self, consecutive_losses: int, cooldown_until: datetime | None = None) -> None:
        """Restore the loss streak (and any still-running cooldown) at startup (§7.7)."""
        self._consecutive_losses = consecutive_losses
        self._cooldown_until = cooldown_until


class RiskEngine:
    """Evaluates every LLM signal against hard-coded risk rules.

    The LLM *never* bypasses this layer. If any rule is violated, the signal
    is rejected with a clear reason.
    """

    def __init__(self, settings: RiskSettings, clock: Clock | None = None) -> None:
        self.settings = settings
        # Injected clock (§7.27): live trading gets SystemClock; replays pass a
        # market-time clock so daily windows/cooldowns follow candle timestamps.
        active_clock = clock or SystemClock()
        self._daily_tracker = DailyLossTracker(active_clock)
        self._loss_tracker = ConsecutiveLossTracker(active_clock)
        # High-water mark for the max-drawdown rule. In-memory only — callers
        # seed it from persisted portfolio history at startup (see
        # ``seed_peak_equity``) so a process restart cannot silently reset the
        # drawdown guard to its most permissive state.
        self._peak_equity: float | None = None

    # ── Public API ────────────────────────────────────────────

    def evaluate(
        self,
        signal: TradeSignal,
        portfolio: PortfolioState,
        planned_notional: float | None = None,
        current_price: float | None = None,
    ) -> RiskResult:
        """Check a trade signal against all risk rules.

        ``planned_notional`` is the *proposed* order size in quote currency
        (quantity × price) as computed by the pipeline; when provided, the
        position-size rule caps it at ``max_position_pct`` of portfolio value —
        so a sizing regression is caught *at* the gate, before execution.

        ``current_price`` (the cycle's mark) enables entry-geometry validation
        (§7.54): stops below/above the wrong side of price, absurdly wide stops,
        and inverted take-profits are rejected instead of auto-closing next cycle.

        Returns ``RiskVerdict.APPROVED`` only if every rule passes.
        """
        # Only check limits for active trades (not HOLD)
        if signal.action == Action.HOLD:
            return RiskResult(verdict=RiskVerdict.APPROVED)

        if signal.action == Action.SELL:
            return self._evaluate_exit(signal, portfolio)

        checks = [
            self._check_confidence(signal),
            self._check_max_positions(signal, portfolio),
            self._check_position_size(signal, portfolio, planned_notional),
            self._check_daily_loss(portfolio),
            self._check_drawdown(portfolio),
            self._check_cooldown(),
            self._check_stop_loss(signal, current_price),
        ]

        for result in checks:
            if result.verdict == RiskVerdict.REJECTED:
                logger.warning("risk_rejected", symbol=signal.symbol, reason=result.reason)
                return result

        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _evaluate_exit(self, signal: TradeSignal, portfolio: PortfolioState) -> RiskResult:
        """SELL = close a held long (spot account — never opens a short), §7.47.

        Closing only *reduces* exposure, so the exposure gates (daily loss, drawdown,
        cooldown, size, max positions) must never strand a position — the same
        rationale as §7.9 exit levels and close-all. Only the confidence rule applies.
        A SELL on a symbol with no long position is refused outright: there is
        nothing to close, and it used to reach the executor as a doomed order.
        """
        if long_exposure(portfolio, signal.symbol) <= 0:
            result = RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=(
                    f"No open long position in {signal.symbol} to sell "
                    "(spot account: SELL closes a position, it never opens a short)"
                ),
            )
        else:
            result = self._check_confidence(signal)
        if result.verdict == RiskVerdict.REJECTED:
            logger.warning("risk_rejected", symbol=signal.symbol, reason=result.reason)
        return result

    def record_outcome(self, was_profitable: bool) -> None:
        """Record trade outcome for consecutive-loss tracking."""
        if was_profitable:
            self._loss_tracker.record_win()
        else:
            self._loss_tracker.record_loss(
                cooldown_minutes=self.settings.consecutive_losses_cooldown_minutes,
                threshold=self.settings.consecutive_losses_threshold,
            )

    def update_daily_value(self, portfolio_value: float) -> None:
        """Update daily loss tracker (and the drawdown high-water mark)."""
        self._daily_tracker.reset_if_new_day(portfolio_value)
        self._daily_tracker.update_latest_value(portfolio_value)
        self.note_equity(portfolio_value)

    def note_equity(self, portfolio_value: float) -> None:
        """Raise the high-water mark if this equity value is a new peak."""
        if portfolio_value > 0 and (
            self._peak_equity is None or portfolio_value > self._peak_equity
        ):
            self._peak_equity = portfolio_value

    def restore_daily_baseline(self, portfolio_value: float) -> None:
        """Rehydrate today's daily-loss baseline from persisted history (§7.7)."""
        self._daily_tracker.restore_today(portfolio_value)

    def restore_loss_streak(
        self, consecutive_losses: int, cooldown_until: datetime | None = None
    ) -> None:
        """Rehydrate the consecutive-loss streak / cooldown from closed outcomes (§7.7)."""
        self._loss_tracker.restore(consecutive_losses, cooldown_until)

    def seed_peak_equity(self, portfolio_value: float | None) -> None:
        """Seed the high-water mark from persisted history at startup.

        The peak lives only in memory otherwise, so a restart would reset the
        drawdown guard. Callers read ``MAX(total_value)`` from stored portfolio
        snapshots and pass it here; later live values only ever raise the peak.
        """
        if portfolio_value is not None:
            self.note_equity(portfolio_value)

    @property
    def peak_equity(self) -> float | None:
        return self._peak_equity

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

    def _check_position_size(
        self,
        signal: TradeSignal,
        portfolio: PortfolioState,
        planned_notional: float | None = None,
    ) -> RiskResult:
        total_value = portfolio.total_value
        if total_value <= 0:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason="Portfolio value is zero or negative",
            )
        cap = self.settings.max_position_pct * total_value
        # Per-*position* cap (§7.42): a BUY adds to what is already held in the
        # symbol, so repeated entries can no longer pyramid one symbol past
        # max_position_pct (12 approved BUYs used to reach 90% of equity).
        existing = long_exposure(portfolio, signal.symbol) if signal.action == Action.BUY else 0.0
        if signal.action == Action.BUY and existing >= cap * (1.0 - 1e-9):
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=(
                    f"Position in {signal.symbol} already at max size "
                    f"({existing:.2f} of cap {cap:.2f}, "
                    f"{self.settings.max_position_pct:.0%} of {total_value:.2f})"
                ),
            )
        # Notional cap at the gate: whatever the pipeline proposes to trade —
        # plus, for a BUY, the position it adds to — must fit inside
        # max_position_pct of portfolio value. Sizing itself lives in the
        # pipeline; this check exists so a sizing regression can never slip
        # past approval.
        if planned_notional is not None:
            resulting = existing + planned_notional
            # Tiny relative tolerance for float rounding in the sizing math;
            # real over-sizing exceeds the cap by orders of magnitude.
            if resulting > cap * (1.0 + 1e-6):
                return RiskResult(
                    verdict=RiskVerdict.REJECTED,
                    reason=(
                        f"Planned position {resulting:.2f} exceeds max position size "
                        f"{cap:.2f} ({self.settings.max_position_pct:.0%} of {total_value:.2f})"
                        + (f"; already holding {existing:.2f}" if existing > 0 else "")
                    ),
                )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_daily_loss(self, portfolio: PortfolioState) -> RiskResult:
        # Only set baseline if not already set (first call of the day).
        # This allows tests and callers to pre-set a start-of-day value.
        if self._daily_tracker.start_of_day_value is None:
            self._daily_tracker.reset_if_new_day(portfolio.total_value)
        self._daily_tracker.update_latest_value(portfolio.total_value)

        pnl_pct = self._daily_tracker.daily_pnl_pct
        # Epsilon tolerance at the boundary (find #3, §7.37), biased *toward*
        # rejection: a drop that float rounding lands a hair short of the cap is
        # still treated as breaching it. Exact-boundary comparisons stop being
        # round-luck while the guard never gets more permissive.
        threshold = self.settings.daily_loss_limit_pct * (1.0 - 1e-9)
        if pnl_pct is not None and pnl_pct <= -threshold:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=f"Daily loss {pnl_pct:.2%} exceeds limit {-self.settings.daily_loss_limit_pct:.2%}",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_drawdown(self, portfolio: PortfolioState) -> RiskResult:
        """Reject while equity is drawdowned past ``max_drawdown_pct`` from its peak.

        The high-water mark follows the portfolio value observed here (and via
        ``update_daily_value``); it is seeded from persisted portfolio history
        at startup so the guard survives restarts. On the first ever reading
        the peak *is* the current value, so no drawdown exists yet.
        """
        total_value = portfolio.total_value
        if total_value <= 0:
            # The zero-value case is owned by _check_position_size; nothing to
            # measure here beyond guarding against a divide-by-zero.
            return RiskResult(verdict=RiskVerdict.APPROVED)
        self.note_equity(total_value)
        peak = self._peak_equity or total_value
        drawdown_pct = (peak - total_value) / peak
        if drawdown_pct > self.settings.max_drawdown_pct:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=(
                    f"Drawdown {drawdown_pct:.2%} exceeds limit "
                    f"{self.settings.max_drawdown_pct:.2%} (peak equity {peak:.2f})"
                ),
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_cooldown(self) -> RiskResult:
        remaining = self._loss_tracker.cooldown_until
        if remaining is not None and self._loss_tracker.in_cooldown:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason=f"In cooldown until {remaining.isoformat()}",
            )
        return RiskResult(verdict=RiskVerdict.APPROVED)

    def _check_stop_loss(
        self, signal: TradeSignal, current_price: float | None = None
    ) -> RiskResult:
        # Entries only. A *close* reduces exposure and the position's own exit
        # levels (§7.9) govern it, so demanding a stop on a sell was nonsense —
        # it blocked legitimate exits while adding no protection.
        if signal.action != Action.BUY:
            return RiskResult(verdict=RiskVerdict.APPROVED)
        if signal.stop_loss is None:
            return RiskResult(
                verdict=RiskVerdict.REJECTED,
                reason="Opening a position requires a stop-loss",
            )

        # Geometry (§7.54): require stop_loss < price < take_profit when the mark
        # is known, and cap how far the stop may sit below price.
        if current_price is not None and current_price > 0:
            if signal.stop_loss >= current_price:
                return RiskResult(
                    verdict=RiskVerdict.REJECTED,
                    reason=(
                        f"Stop-loss {signal.stop_loss:g} is not below the current price "
                        f"{current_price:g} — the entry would breach its own stop immediately"
                    ),
                )
            if signal.take_profit is not None and signal.take_profit <= current_price:
                return RiskResult(
                    verdict=RiskVerdict.REJECTED,
                    reason=(
                        f"Take-profit {signal.take_profit:g} is not above the current price "
                        f"{current_price:g} — it would 'take profit' at a loss on the next tick"
                    ),
                )
            stop_distance_pct = (current_price - signal.stop_loss) / current_price
            if stop_distance_pct > self.settings.max_stop_distance_pct:
                return RiskResult(
                    verdict=RiskVerdict.REJECTED,
                    reason=(
                        f"Stop distance {stop_distance_pct:.1%} exceeds "
                        f"risk.max_stop_distance_pct ({self.settings.max_stop_distance_pct:.1%}) "
                        "— such a stop carries unbounded per-trade risk"
                    ),
                )
        return RiskResult(verdict=RiskVerdict.APPROVED)
