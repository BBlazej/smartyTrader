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
    quantity: float
    avg_entry_price: float
    current_price: float

    @property
    def pnl(self) -> float:
        return (self.current_price - self.avg_entry_price) * self.quantity

    @property
    def pnl_pct(self) -> float:
        if self.avg_entry_price == 0:
            return 0.0
        return (self.current_price - self.avg_entry_price) / self.avg_entry_price


class PortfolioState(BaseModel):
    cash: float
    positions: list[Position] = []

    @property
    def total_value(self) -> float:
        position_value = sum(p.quantity * p.current_price for p in self.positions)
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
    ) -> OrderResult: ...

    async def get_positions(self) -> list[Position]: ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def get_cash(self) -> float: ...

    async def close(self) -> None: ...
