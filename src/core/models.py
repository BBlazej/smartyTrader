"""Shared data models used across the trading agent."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ── LLM Signal ────────────────────────────────────────────────


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class TradeSignal(BaseModel):
    """Structured output from the LLM decision engine."""

    symbol: str
    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    stop_loss: float | None = None
    take_profit: float | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # True only for the safe HOLD fallback the LLM client returns when all
    # retries were exhausted. Fallback rows are persisted for audit but never
    # re-fed into later prompts (§7.8).
    is_fallback: bool = False


# ── Decision History ──────────────────────────────────────────


class DecisionRecord(BaseModel):
    """A single prior decision, fed back to the LLM so it can learn from its
    own track record (the action taken, the risk verdict, and the reasoning).

    Built from a stored :class:`LLMDecisionRow` by the decision pipeline and
    rendered into the user prompt by :func:`build_user_prompt`.
    """

    action: str  # buy / sell / hold
    confidence: float
    reasoning: str
    risk_verdict: str  # approved / rejected / unknown
    risk_reason: str | None = None
    realized_pnl: float | None = None  # Net PnL once the position closed (None = still open)
    timestamp: datetime | None = None


# ── Risk Verdict ──────────────────────────────────────────────


class PositionSide(str, Enum):
    """Which way a position is exposed (§7.38). Spot-only today — everything
``long``; the ``short`` half exists so margin/derivatives work can build on an
honest model instead of encoding shorts as positive-quantity longs (find #4)."""

    LONG = "long"
    SHORT = "short"


class RiskVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskResult(BaseModel):
    """Deterministic risk engine verdict."""

    verdict: RiskVerdict
    reason: str | None = None  # Why rejected (None if approved)


# ── Portfolio State ───────────────────────────────────────────


class Position(BaseModel):
    symbol: str
    quantity: float  # always the absolute size; direction lives in ``side``
    avg_entry_price: float
    current_price: float
    # §7.38 (find #4): explicit direction. Defaults to long, so every existing
    # spot path and stored snapshot keeps working unchanged; a short maps here
    # with POSITIVE quantity and side=SHORT — never a fake long.
    side: PositionSide = PositionSide.LONG
    # Deterministic exit levels carried from the entry signal (§7.9). The pipeline
    # closes the position when the mark price breaches them, without consulting
    # the LLM. ``None`` = level not set. Stored with portfolio snapshots, so they
    # survive restarts.
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def _direction(self) -> float:
        return -1.0 if self.side == PositionSide.SHORT else 1.0

    @property
    def pnl(self) -> float:
        # Shorts gain when price falls below entry (§7.38).
        return (self.current_price - self.avg_entry_price) * self.quantity * self._direction

    @property
    def pnl_pct(self) -> float:
        if self.avg_entry_price == 0:
            return 0.0
        return (self.current_price - self.avg_entry_price) / self.avg_entry_price * self._direction


class PortfolioState(BaseModel):
    cash: float
    positions: list[Position] = []

    @property
    def total_value(self) -> float:
        # Shorts are carried as a liability at the close price (proceeds of the
        # opening sell already sit in cash) — §7.38.
        position_value = sum(
            p.quantity * p.current_price * (-1.0 if p.side == PositionSide.SHORT else 1.0)
            for p in self.positions
        )
        return self.cash + position_value

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.pnl for p in self.positions)


# ── Market Snapshot ───────────────────────────────────────────


class OHLCV(BaseModel):
    """Single candle."""

    timestamp: datetime | None = None
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketSnapshot(BaseModel):
    symbol: str
    timeframe: str  # e.g. "1h", "4h"
    candles: list[OHLCV] = []
    indicators: dict[str, Any] = {}  # RSI, MACD, etc.
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Order Result ──────────────────────────────────────────────


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ClosedEntry(BaseModel):
    """One entry decision's share of a closing sell's realized PnL (§7.8).

    Produced by the shared FIFO :class:`~src.execution.position_tracker.PositionTracker`
    so the agent can backfill the *originating buy* decisions — not just the
    sell row — once a position closes.
    """

    entry_decision_id: int | None = None
    pnl: float  # net of tracked fees for the consumed lot(s)


class OrderResult(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float | None = None  # None for market orders before fill
    status: str  # "filled", "pending", "rejected", etc.
    filled_at: datetime | None = None
    reason: str | None = None  # Explanation when rejected
    realized_pnl: float | None = None  # PnL realized by this order (set on a closing sell)
    # Per-entry-decision attribution of realized_pnl (set on closing fills).
    closed_entries: list[ClosedEntry] = []


# ── Executor Protocol ───────────────────────────────────────


@runtime_checkable
class Executor(Protocol):
    """Swappable order execution interface.

    Every executor (paper, Kraken testnet, XTB demo) implements this contract.
    The risk engine and decision pipeline depend only on this interface.
    """

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
        decision_id: int | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        """Place an order. ``decision_id`` links the order (and, via the shared
        FIFO tracker, its closing fills' ``closed_entries``) to the LLM decision
        that produced it (§7.8). ``stop_loss``/``take_profit`` are the entry
        signal's exit levels, attached to the resulting position so the pipeline
        can enforce them deterministically on later cycles (§7.9)."""
        ...

    async def get_positions(self) -> list[Position]: ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def get_cash(self) -> float: ...

    async def close(self) -> None: ...
