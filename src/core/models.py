"""Shared data models used across the trading agent."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, StringConstraints

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
    realized_pnl: float | None = None  # Net PnL once the position closed (None = not closed)
    timestamp: datetime | None = None
    # Whether an order placed for this decision filled (§7.45) — lets the prompt tell
    # "still open" from "never traded". ``None`` = not looked up / unknown.
    filled: bool | None = None


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
    # Cash committed to venue BUY orders the ledger has not booked yet — working at
    # the venue, or filled but unconfirmed (a status-poll timeout). Counted in
    # ``total_value`` so equity doesn't dip by the order's notional until
    # reconciliation catches up (§7.79), but never spendable: sizing reads ``cash``.
    pending_value: float = 0.0

    @property
    def total_value(self) -> float:
        # Shorts are carried as a liability at the close price (proceeds of the
        # opening sell already sit in cash) — §7.38.
        position_value = sum(
            p.quantity * p.current_price * (-1.0 if p.side == PositionSide.SHORT else 1.0)
            for p in self.positions
        )
        return self.cash + self.pending_value + position_value

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


class BookContext(BaseModel):
    """The agent's own book for one symbol, rendered into the prompt (§7.45).

    Without it the LLM could not tell opening from adding to a position, and issued
    sells on symbols it did not hold. ``position`` is ``None`` when flat.
    """

    position: Position | None = None
    cash: float
    total_value: float
    max_position_pct: float | None = None
    # Strategy sleeve deciding (§7.71) and its holding limit; ``held_hours`` is how
    # long this symbol's position has been open (``None`` when flat/unknown).
    strategy: str | None = None
    max_holding_hours: float | None = None
    held_hours: float | None = None


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
    # Fees the venue reported for this fill, in the pair's base / quote currency
    # (§7.77). Persisted on the order row so a restart replays the ledger net of them.
    fee_base: float | None = None
    fee_quote: float | None = None


# ── Executor Protocol ───────────────────────────────────────


@runtime_checkable
class Executor(Protocol):
    """Swappable order execution interface.

    Every executor (paper, ccxt spot, XTB demo) implements this contract.
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


# ── Market context (§7.18, CHANGE.md §4.4 / P5) ─────────────


class EventKind(str, Enum):
    """What a :class:`MarketEvent` is — only ``delisting``/``earnings``/``macro`` gate."""

    MACRO = "macro"  # scheduled economic release / central-bank decision
    EARNINGS = "earnings"  # a stock's earnings release
    DELISTING = "delisting"  # the venue announced it will delist the asset


class EventImportance(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


class MarketEvent(BaseModel):
    """A dated external event — calendar data, never LLM text (CHANGE.md §4.4).

    ``asset`` is the base asset / ticker (``BTC`` for ``BTC/EUR``, ``AAPL``) the event
    concerns; ``None`` = market-wide (a macro release, which carries ``currency``
    instead — the economy it belongs to). ``at`` is the scheduled time, or for a
    delisting the notice's publication time. The deterministic event guard in the
    risk engine reads these rows directly.
    """

    source: str = Field(max_length=20)
    kind: EventKind
    at: datetime
    title: str = Field(max_length=200)
    asset: str | None = Field(default=None, max_length=20)
    currency: str | None = Field(default=None, max_length=10)
    importance: EventImportance = EventImportance.HIGH
    url: str | None = Field(default=None, max_length=500)

    def dedup_key(self) -> str:
        """Stable identity across refreshes (same event fetched twice = one row)."""
        import hashlib

        at = self.at.astimezone(UTC) if self.at.tzinfo else self.at.replace(tzinfo=UTC)
        raw = "|".join(
            [
                self.source,
                self.kind.value,
                self.asset or "",
                self.currency or "",
                at.strftime("%Y-%m-%dT%H:%M"),
                self.title.strip().lower(),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class SentimentReading(BaseModel):
    """One market-wide sentiment reading (e.g. the crypto Fear & Greed index, 0–100)."""

    source: str = Field(max_length=20)
    value: float
    label: str | None = Field(default=None, max_length=40)
    as_of: datetime


class NewsItem(BaseModel):
    """One ingested news/filing item (§7.18). Raw text — never enters a trading prompt.

    Only the batch summarizer reads ``text``; the trading prompt sees the validated
    :class:`ContextCard` built from it (CHANGE.md §7: prompt-injection mitigation).
    """

    source: str = Field(max_length=40)
    url: str = Field(max_length=1000)
    title: str = Field(max_length=300)
    text: str = ""
    published_at: datetime
    symbols: list[str] = []

    def content_hash(self) -> str:
        """Dedup key: the same article from two feeds (or two refreshes) is one item."""
        import hashlib

        raw = f"{self.url.strip()}|{self.title.strip().lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class CardEvent(BaseModel):
    """An upcoming event the summarizer noticed in the news (informational only)."""

    model_config = {"extra": "forbid"}

    type: str = Field(pattern=r"^(earnings|macro|listing|delisting|regulatory|upgrade|other)$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ContextCard(BaseModel):
    """Per-symbol news digest produced by the batch summarizer (CHANGE.md §4.4).

    Strict and bounded like :class:`TradeSignal`: unknown fields are rejected, every
    string is length-capped and ``sources`` must cite the fed items' URLs (checked by
    :func:`src.analysis.context_cards.parse_context_card`). The trading prompt renders
    only these structured fields. Event *guards* never read a card — they use calendar
    data (:class:`MarketEvent`).
    """

    model_config = {"extra": "forbid"}

    symbol: str = Field(max_length=20)
    as_of: datetime
    sentiment: float = Field(ge=-1.0, le=1.0)
    catalysts: list[Annotated[str, StringConstraints(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=5
    )
    event_risk: list[CardEvent] = Field(default_factory=list, max_length=5)
    sources: list[Annotated[str, StringConstraints(max_length=1000)]] = Field(
        min_length=1, max_length=8
    )
    confidence: float = Field(ge=0.0, le=1.0)


class SymbolContext(BaseModel):
    """Everything external the pipeline knows about one symbol at decision time (§7.18).

    Built by :class:`src.core.context.ContextReader`: fresh sentiment (``None`` when
    stale/missing), scheduled events near ``now``, venue notices (delistings) and the
    latest unexpired context card. Rendered into the prompt's MARKET CONTEXT section
    and checked by :meth:`RiskEngine.check_event_guard`.
    """

    symbol: str
    now: datetime
    lookahead_hours: float = 48.0  # upcoming-events horizon shown in the prompt
    sentiment: SentimentReading | None = None
    events: list[MarketEvent] = []  # macro + earnings in the reader's window, by time
    notices: list[MarketEvent] = []  # delisting notices for this asset
    card: ContextCard | None = None
