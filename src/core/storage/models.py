"""SQLAlchemy row models for the storage layer (§7.36 split of ``core/storage.py``).

All tables live here; the :class:`Storage` facade composes its method mixins on top
of these models. Timestamps are stored via SQLite DATETIME (naive UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _as_naive_utc(value: datetime) -> datetime:
    """Render an (aware or naive) UTC datetime as naive UTC for SQLite comparison.

    Timestamps are stored via SQLAlchemy's SQLite DATETIME, which drops tz info —
    so range comparisons must use the same naive-UTC wall clock.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


# ── Base ──────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Models ────────────────────────────────────────────────────


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    timeframe: Mapped[str] = mapped_column(String(10))
    candles_json: Mapped[str] = mapped_column(Text)  # JSON array of OHLCV dicts
    indicators_json: Mapped[str] = mapped_column(Text, default="{}")
    fetched_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class LLMDecisionRow(Base):
    __tablename__ = "llm_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(10))  # buy / sell / hold
    confidence: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str] = mapped_column(Text)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_verdict: Mapped[str] = mapped_column(String(10))  # approved / rejected
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Net PnL once the position closed (None = still open)
    # True for LLM-unavailable HOLD fallbacks — persisted for audit, excluded
    # from prompt context (§7.8).
    is_fallback: Mapped[bool] = mapped_column(default=False)
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    # Owning agent (§7.39): ``crypto`` / ``stocks``. Both agents share one DB, so every
    # read that feeds an agent's own state (prompt history, loss-streak rehydration)
    # filters on it. NULL only for rows written by an unbound Storage (tests/tools).
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(10))  # buy / sell
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Storage time for retention pruning (§7.12): ``filled_at`` is only set on
    # fills, so it cannot bound the age of pending/rejected/cancelled rows.
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    # Owning agent (§7.39) — the FIFO replay (§7.25) must only see this agent's fills.
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Realized PnL of a *closing* fill (§7.46) — one row per closing fill, exactly
    # what the live loss-streak tracker counts, so restart rehydration matches it.
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


class PortfolioSnapshotRow(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    cash: Mapped[float] = mapped_column(Float)
    positions_json: Mapped[str] = mapped_column(Text, default="[]")
    total_value: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    # Owning agent (§7.39): each agent's book, daily baseline and drawdown peak are
    # its own — mixing them restored one agent's positions into the other's executor.
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)


class DrawdownResetRow(Base):
    """Audited drawdown peak re-baseline per agent (§7.53).

    Without one, the high-water mark is MAX over never-pruned snapshots — a permanent
    latch whose only exit would be hand-editing SQLite. This row records the operator's
    explicit CLI reset: new baseline value + when it happened. Startup seeding then
    ignores snapshots older than ``reset_at`` (they stay in the DB as audit history).
    """

    __tablename__ = "drawdown_resets"

    agent: Mapped[str] = mapped_column(String(20), primary_key=True)  # crypto | stocks
    baseline_value: Mapped[float] = mapped_column(Float)
    reset_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class AgentControlRow(Base):
    """Control-plane row per agent (§7.15): the DB stays the single source of truth.

    The dashboard/control API *write* intent here (pause, close-all, safe config
    overrides); the agent re-reads it at the top of every cycle (a cheap SQLite
    read) and carries out the actions. One row per agent, keyed by name.
    """

    __tablename__ = "agent_control"

    agent: Mapped[str] = mapped_column(String(20), primary_key=True)  # crypto | stocks
    state: Mapped[str] = mapped_column(String(10), default="running")  # running | paused
    close_all_requested: Mapped[bool] = mapped_column(default=False)
    last_cycle_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Safe config overrides (whitelist-validated JSON) applied by the agent each
    # cycle; NULL/empty means plain settings.yaml. Credentials are never stored.
    config_override_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
