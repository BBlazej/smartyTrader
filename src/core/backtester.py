"""Decision-replay backtester (§7.14).

Re-simulates **stored decisions** against the price path that followed, through the
same :class:`RiskEngine` and the same fee/slippage model (:class:`PaperExecutor`) as
live — deterministic, zero LLM calls. (LLM replay — feeding history to the model for
fresh signals — is a separate, later experiment: non-deterministic + costly locally.)

Fidelity contract with live (§7.1/§7.5/§7.9): every candle event re-marks open
positions at its close; exit levels are enforced on breach *without* the risk gate;
entries are sized by the shared :func:`calculate_quantity` and gated by
``RiskEngine.evaluate`` with ``planned_notional`` — what the gate approves is what
fills. Decisions replay in timestamp order across symbols against one shared book.

The risk engine runs on a **timeline clock** (§7.27): its daily-loss windows and
cooldowns advance with candle/decision timestamps, so a multi-month replay gets
real per-day caps instead of one continuous wall-clock "today".

Known simplifications (documented deliberately):
* Equity-curve Sharpe annualizes by the candle timeframe's nominal periods/year;
  stocks have weekends/gaps, so it is an approximation.

Metrics: total return vs buy-and-hold benchmark, win rate, avg win/loss, max drawdown,
Sharpe, per-symbol breakdown. The CLI lives in ``scripts/backtest.py``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import pairwise

import structlog

from ..execution.paper_executor import PaperExecutor
from .config import RiskSettings
from .decision_pipeline import calculate_quantity, exit_level_breach
from .models import OHLCV, Action, OrderSide, PortfolioState, TradeSignal
from .risk_engine import RiskEngine

logger = structlog.get_logger()


class TimelineClock:
    """A :class:`~src.core.risk_engine.Clock` driven by the replay timeline (§7.27).

    ``now()`` returns the timestamp of the most recently applied event, so the
    risk engine's day boundaries and cooldown expiries follow market time. Before
    the first event it falls back to wall-clock now (no state exists yet anyway).
    """

    def __init__(self) -> None:
        self._current: datetime | None = None

    def set(self, ts: datetime) -> None:
        self._current = ts

    def now(self) -> datetime:
        return self._current if self._current is not None else datetime.now(UTC)

# Nominal candle periods per year for Sharpe annualization (crypto runs 24/7; stock
# series have gaps — approximation documented above).
_PERIODS_PER_YEAR: dict[str, float] = {
    "1m": 525_600.0,
    "5m": 105_120.0,
    "15m": 35_040.0,
    "30m": 17_520.0,
    "1h": 8_760.0,
    "4h": 2_190.0,
    "1d": 365.0,
    "1w": 52.0,
}


@dataclass(frozen=True)
class ReplayDecision:
    """One stored decision to replay (decoupled from the storage row type)."""

    timestamp: datetime
    symbol: str
    action: str  # "buy" | "sell" | "hold"
    confidence: float
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class SymbolStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0


@dataclass
class BacktestReport:
    start: str
    end: str
    symbols: list[str]
    timeframe: str
    initial_cash: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    closed_trades: int
    auto_exits: int
    risk_rejected: int
    holds: int
    buy_and_hold_pct: dict[str, float]
    blended_buy_and_hold_pct: float | None
    per_symbol: dict[str, dict[str, float | int]]
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class DecisionReplayBacktester:
    """Replays stored decisions over historical candles with the live risk/fee model."""

    def __init__(
        self,
        *,
        risk_settings: RiskSettings,
        initial_cash: float,
        fee_pct: float = 0.0,
        slippage_pct: float = 0.0,
    ) -> None:
        # Fresh engine/executor per run: the replay must never observe live state, and
        # two runs must not share trackers. The engine's clock follows the replay
        # timeline, not the wall clock of the run (§7.27).
        self._clock = TimelineClock()
        self._risk_engine = RiskEngine(risk_settings, clock=self._clock)
        self._settings = risk_settings
        self._executor = PaperExecutor(
            initial_cash=initial_cash,
            slippage_pct=slippage_pct,
            fee_pct=fee_pct,
        )
        self._initial_cash = initial_cash
        self._wins = 0
        self._losses = 0
        self._win_amounts: list[float] = []
        self._loss_amounts: list[float] = []
        self._auto_exits = 0
        self._risk_rejected = 0
        self._holds = 0
        self._per_symbol: dict[str, SymbolStats] = {}
        # Latest candle close per symbol as the timeline walks — lets decisions price
        # symbols with no open position.
        self._last_close: dict[str, float] = {}

    # ── Public API ────────────────────────────────────────────

    async def replay(
        self,
        decisions: list[ReplayDecision],
        candles_by_symbol: dict[str, list[OHLCV]],
        *,
        timeframe: str = "1d",
    ) -> BacktestReport:
        """Replay ``decisions`` over ``candles_by_symbol`` (both may interleave symbols).

        Candle lists must be sorted oldest-first. Decisions are replayed in
        timestamp order; each is evaluated against the book as it stands at that
        moment — after every earlier candle and trade.
        """
        events = self._build_timeline(decisions, candles_by_symbol)
        equity_curve: list[tuple[datetime, float]] = (
            [(events[0][0], self._initial_cash)] if events else []
        )

        for ts, kind, payload in events:
            self._clock.set(ts)  # risk engine day/cooldown time follows the timeline
            if kind == "candle":
                symbol, candle = payload
                await self._on_candle(symbol, candle)
            else:
                await self._on_decision(payload)
            value = await self._total_value()
            # Live parity: BaseTradingAgent feeds every cycle's equity into the daily
            # tracker; per-event here advances day windows under the timeline clock.
            self._risk_engine.update_daily_value(value)
            equity_curve.append((ts, value))

        # Drop consecutive duplicate points (same mark → no information, skews Sharpe).
        curve = _dedupe_consecutive(equity_curve)
        return self._build_report(curve, candles_by_symbol, timeframe)

    # ── Event handling ────────────────────────────────────────

    async def _on_candle(self, symbol: str, candle: OHLCV) -> None:
        """Mark this symbol's position at the candle close; enforce exit levels (§7.9)."""
        if candle.close <= 0:
            return
        self._last_close[symbol] = candle.close
        self._executor.update_price(symbol, candle.close)

        if not self._settings.enforce_exit_levels:
            return
        position = next(
            (
                p
                for p in await self._executor.get_positions()
                if p.symbol == symbol and p.quantity > 0
            ),
            None,
        )
        if position is None:
            return
        reason = exit_level_breach(position, candle.close)
        if reason is None:
            return
        self._auto_exits += 1
        await self._fill(
            TradeSignal(
                symbol=symbol,
                action=Action.SELL,
                confidence=0.0,
                reasoning=f"replay exit level: {reason}",
            ),
            OrderSide.SELL,
            position.quantity,
            candle.close,
        )

    async def _on_decision(self, decision: ReplayDecision) -> None:
        if decision.action == Action.HOLD.value:
            self._holds += 1
            return

        price = await self._mark_price(decision.symbol)
        if price is None or price <= 0:
            self._risk_rejected += 1  # unpriceable → nothing was traded
            logger.warning("replay skipped decision (no price yet)", symbol=decision.symbol)
            return

        signal = TradeSignal(
            symbol=decision.symbol,
            action=Action(decision.action),
            confidence=decision.confidence,
            reasoning="replay",
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
        )
        portfolio = await self._portfolio_state()
        quantity = calculate_quantity(signal, portfolio, self._settings, price)
        planned_notional = quantity * price

        risk = self._risk_engine.evaluate(signal, portfolio, planned_notional=planned_notional)
        if risk.verdict.value == "rejected":
            self._risk_rejected += 1
            return

        order = await self._fill(signal, _order_side(signal.action), quantity, price)
        if order is not None and order.status != "filled":
            # Executor-side rejection (e.g. insufficient cash) — mirrors live where the
            # pipeline records the rejected OrderResult; nothing further to track.
            logger.debug(
                "replay order rejected by executor", symbol=decision.symbol, reason=order.reason
            )

    async def _fill(self, signal: TradeSignal, side: OrderSide, quantity: float, price: float):
        order = await self._executor.place_order(
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            price=price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )
        if order.status == "filled" and order.realized_pnl is not None:
            self._record_closed_trade(signal.symbol, order.realized_pnl)
            # Same rule as live: only real closed outcomes feed the loss streak (§7.8).
            self._risk_engine.record_outcome(was_profitable=order.realized_pnl >= 0)
        return order

    def _record_closed_trade(self, symbol: str, pnl: float) -> None:
        stats = self._per_symbol.setdefault(symbol, SymbolStats())
        stats.trades += 1
        stats.realized_pnl += pnl
        if pnl >= 0:
            self._wins += 1
            stats.wins += 1
            self._win_amounts.append(pnl)
        else:
            self._losses += 1
            stats.losses += 1
            self._loss_amounts.append(pnl)

    # ── Bookkeeping helpers ───────────────────────────────────

    async def _portfolio_state(self) -> PortfolioState:
        return PortfolioState(
            cash=await self._executor.get_cash(),
            positions=await self._executor.get_positions(),
        )

    async def _total_value(self) -> float:
        return (await self._portfolio_state()).total_value

    async def _mark_price(self, symbol: str) -> float | None:
        """Latest mark for *symbol*: an open position's price, else its last candle."""
        positions = await self._executor.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is not None and position.current_price > 0:
            return position.current_price
        return self._last_close.get(symbol)

    def _build_timeline(
        self,
        decisions: list[ReplayDecision],
        candles_by_symbol: dict[str, list[OHLCV]],
    ) -> list[tuple[datetime, str, object]]:
        """Merge candles and decisions into one timestamp-ordered event stream.

        Same-timestamp ordering puts candles *before* decisions — mirroring a live
        cycle (fetch snapshot → decide against its last close).
        """
        events: list[tuple[datetime, int, str, object]] = []
        self._last_close = {}
        for symbol, candles in candles_by_symbol.items():
            for candle in candles:
                if candle.timestamp is None or candle.close <= 0:
                    continue
                events.append((candle.timestamp, 0, "candle", (symbol, candle)))
        for decision in decisions:
            ts = decision.timestamp
            if ts.tzinfo is None:  # stored naive UTC → localize for uniform ordering
                ts = ts.replace(tzinfo=UTC)
            events.append((ts, 1, "decision", decision))
        events.sort(key=lambda e: (e[0], e[1]))
        return [(ts, kind, payload) for ts, _o, kind, payload in events]

    def _build_report(
        self,
        curve: list[tuple[datetime, float]],
        candles_by_symbol: dict[str, list[OHLCV]],
        timeframe: str,
    ) -> BacktestReport:
        final_equity = curve[-1][1] if curve else self._initial_cash
        total_return_pct = (
            (final_equity - self._initial_cash) / self._initial_cash * 100.0
            if self._initial_cash > 0
            else 0.0
        )
        max_dd = max_drawdown([value for _, value in curve])
        sharpe = sharpe_ratio(
            [value for _, value in curve], _PERIODS_PER_YEAR.get(timeframe, 365.0)
        )

        closed = self._wins + self._losses
        win_rate = self._wins / closed if closed else None

        buy_hold: dict[str, float] = {}
        for symbol, candles in candles_by_symbol.items():
            usable = [c for c in candles if c.close > 0]
            if len(usable) >= 2 and usable[0].close > 0:
                buy_hold[symbol] = (usable[-1].close / usable[0].close - 1.0) * 100.0
        blended = sum(buy_hold.values()) / len(buy_hold) if buy_hold else None

        return BacktestReport(
            start=curve[0][0].isoformat() if curve else "",
            end=curve[-1][0].isoformat() if curve else "",
            symbols=sorted(candles_by_symbol),
            timeframe=timeframe,
            initial_cash=self._initial_cash,
            final_equity=round(final_equity, 8),
            total_return_pct=round(total_return_pct, 6),
            max_drawdown_pct=round(max_dd * 100.0, 6),
            sharpe=round(sharpe, 6),
            win_rate=round(win_rate, 6) if win_rate is not None else None,
            avg_win=round(sum(self._win_amounts) / len(self._win_amounts), 8)
            if self._win_amounts
            else None,
            avg_loss=round(sum(self._loss_amounts) / len(self._loss_amounts), 8)
            if self._loss_amounts
            else None,
            closed_trades=closed,
            auto_exits=self._auto_exits,
            risk_rejected=self._risk_rejected,
            holds=self._holds,
            buy_and_hold_pct={s: round(v, 6) for s, v in buy_hold.items()},
            blended_buy_and_hold_pct=round(blended, 6) if blended is not None else None,
            per_symbol={
                s: {
                    "trades": st.trades,
                    "wins": st.wins,
                    "losses": st.losses,
                    "realized_pnl": round(st.realized_pnl, 8),
                }
                for s, st in sorted(self._per_symbol.items())
            },
            equity_curve=[(ts.isoformat(), round(value, 8)) for ts, value in curve],
        )


# ── Pure metric helpers (unit-tested directly) ────────────────


def max_drawdown(values: list[float]) -> float:
    """Peak-to-trough drawdown fraction of an equity curve (0.0 when rising)."""
    peak = float("-inf")
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return max_dd


def sharpe_ratio(values: list[float], periods_per_year: float) -> float:
    """Annualized Sharpe of period returns between consecutive equity points.

    A coarse-but-honest v1: arithmetic mean / sample std of simple returns scaled by
    √periods-per-year. Fewer than 3 points (or no dispersion) ⇒ 0.0.
    """
    if len(values) < 3:
        return 0.0
    returns: list[float] = []
    for prev, nxt in pairwise(values):
        if prev > 0:
            returns.append(nxt / prev - 1.0)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std <= 1e-15:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def _dedupe_consecutive(points: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for point in points:
        if not out or abs(point[1] - out[-1][1]) > 1e-12 or point[0] != out[-1][0]:
            out.append(point)
    return out


def _order_side(action: Action) -> OrderSide:
    return OrderSide.BUY if action == Action.BUY else OrderSide.SELL
