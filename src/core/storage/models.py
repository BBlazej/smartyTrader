"""SQLAlchemy row models for the storage layer (§7.36 split of ``core/storage.py``).

All tables live here; the :class:`Storage` facade composes its method mixins on top
of these models. Timestamps are stored via SQLite DATETIME (naive UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
    # Per-decision LLM cost profile (§7.69): whole-call latency incl. retries and
    # the completion's usage counts (NULL when the server omits them).
    llm_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    llm_completion_tokens: Mapped[int | None] = mapped_column(nullable=True)
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    # Owning agent (§7.39): ``crypto`` / ``stocks``. Both agents share one DB, so every
    # read that feeds an agent's own state (prompt history, loss-streak rehydration)
    # filters on it. NULL only for rows written by an unbound Storage (tests/tools).
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Strategy sleeve that made the decision (§7.71). NULL = no sleeves (the single
    # implicit style) or pre-§7.71 history. Sleeve prompt history and bar timing
    # filter on it; position ownership (symbol lock, time stops) derives from it.
    strategy: Mapped[str | None] = mapped_column(String(20), nullable=True)


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
    # Execution venue (§7.61): ``paper`` / ``myokx-sandbox`` / ``xtb-demo`` … — each
    # executor replays only its own fills at restart (runners always stamp it).
    venue: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Sleeve the order was placed for (§7.71): the entry's sleeve, or the owning
    # sleeve of the position a close reduced. NULL without sleeves.
    strategy: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Fees the venue reported for the fill (§7.77): base-currency (OKX spot BUYs pay in
    # the coin) and quote-currency. The restart replay books lots net of them exactly
    # like the live path; NULL = none reported / legacy row.
    fee_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_quote: Mapped[float | None] = mapped_column(Float, nullable=True)


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
    # Execution venue (§7.61): the paper book is restored only from paper snapshots —
    # never from a venue account's cash/positions.
    venue: Mapped[str | None] = mapped_column(String(40), nullable=True)


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
    # The venue whose peak was re-baselined (§7.76) — a paper reset never moves a
    # keyed account's latch. One row per agent: the latest
    # reset wins, whichever venue it was for.
    venue: Mapped[str | None] = mapped_column(String(40), nullable=True)


class WatchlistEntryRow(Base):
    """A dynamic watchlist symbol added by the screener (§7.70).

    Core YAML symbols are *not* stored here — they always stay in the traded set.
    Only manager-added entries carry a TTL (``expires_at``): when it passes, the
    entry is deleted on the next refresh and its slot frees up for a new candidate.
    ``meta_json`` records the ranking inputs (volume/momentum/volatility) at add
    time for audit. Agent-scoped like the trade tables (§7.39 pattern).
    """

    __tablename__ = "watchlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(10), default="screener")
    added_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime] = mapped_column()
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


class StrategyAllocationRow(Base):
    """Audited capital allocation across an agent's strategy sleeves (§7.71).

    ``base_equity`` is the agent's cost-basis equity (cash + open positions at their
    entry price) when the allocation was made; each sleeve's capital is
    ``weight × base_equity``. With fixed weights a new row is written only when the
    configured weights change (or on the first sleeve run); the allocator (CHANGE.md
    P3) will append rows the same way. Never pruned — sleeve equity is measured from
    the latest row, per agent and venue.
    """

    __tablename__ = "strategy_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    venue: Mapped[str | None] = mapped_column(String(40), nullable=True)
    base_equity: Mapped[float] = mapped_column(Float)
    weights_json: Mapped[str] = mapped_column(Text)  # {"crypto_swing": 0.5, ...}
    reason: Mapped[str] = mapped_column(String(40), default="initial")
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class SleeveSnapshotRow(Base):
    """One sleeve's equity at the end of a cycle (§7.71, CHANGE.md §4.8).

    The per-sleeve twin of ``portfolio_snapshots``: the first row of the UTC day
    rehydrates the sleeve's daily-loss baseline and MAX(equity) since the latest
    allocation seeds its drawdown high-water mark — so both survive restarts.
    Never pruned (same reason as portfolio snapshots).
    """

    __tablename__ = "sleeve_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    venue: Mapped[str | None] = mapped_column(String(40), nullable=True)
    strategy: Mapped[str] = mapped_column(String(20), index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    positions_value: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # since the allocation
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class SleeveDrawdownResetRow(Base):
    """Audited drawdown peak re-baseline for one strategy sleeve (§7.71, as §7.53).

    Keyed per (agent, sleeve). Startup seeding of the sleeve's high-water mark then
    ignores sleeve snapshots older than ``reset_at`` — the operator's CLI-only exit
    from a latched sleeve drawdown guard (``rebaseline_drawdown.py --strategy``).
    """

    __tablename__ = "sleeve_drawdown_resets"

    agent: Mapped[str] = mapped_column(String(20), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(20), primary_key=True)
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


class DbIdentityRow(Base):
    """Which book a database file holds — one row, written when the file is created (§7.78).

    Each agent × trading mode has its own file (:mod:`src.core.db_layout`). A runner
    opens its file with the identity it expects, and :class:`~src.core.storage.Storage`
    refuses a mismatch — a real-money run can never write into a paper file, nor a
    paper run into a real one — and refuses a legacy file with data but no identity.
    """

    __tablename__ = "db_identity"

    id: Mapped[int] = mapped_column(primary_key=True)  # always 1
    agent: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(10))  # paper | demo | real
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


# ── Market context (§7.18, CHANGE.md §4.4 / §4.6) ─────────────


class MarketEventRow(Base):
    """A dated external event (macro release, earnings, venue delisting notice).

    Calendar data, never LLM text: the deterministic event guard reads these rows.
    ``asset`` NULL = market-wide (macro, identified by ``currency``). ``dedup_key``
    (:meth:`MarketEvent.dedup_key`) makes refreshes idempotent. Agent-scoped (§7.39).
    """

    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))
    asset: Mapped[str | None] = mapped_column(String(20), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    at: Mapped[datetime] = mapped_column(index=True)
    importance: Mapped[str] = mapped_column(String(10), default="high")
    title: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(32), index=True)
    fetched_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class SentimentReadingRow(Base):
    """A market-wide sentiment reading (e.g. crypto Fear & Greed), one per source × as_of."""

    __tablename__ = "sentiment_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20))
    value: Mapped[float] = mapped_column(Float)
    label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    as_of: Mapped[datetime] = mapped_column(index=True)
    fetched_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class NewsItemRow(Base):
    """An ingested news/filing item (§7.18). Its raw text only ever feeds the summarizer.

    ``symbols_json`` lists the traded symbols the item was matched to at ingest;
    ``content_hash`` (:meth:`NewsItem.content_hash`) dedups across feeds/refreshes.
    """

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(index=True)
    symbols_json: Mapped[str] = mapped_column(Text, default="[]")
    content_hash: Mapped[str] = mapped_column(String(32), index=True)
    fetched_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class ContextCardRow(Base):
    """A validated per-symbol context card from the batch summarizer (§7.18).

    ``card_json`` is the :class:`ContextCard` exactly as validated; ``expires_at``
    is its TTL — the pipeline never shows an expired card. ``news_through`` is the
    newest item's publish time the card covered, so the summarizer knows when a
    symbol has fresh news to digest.
    """

    __tablename__ = "context_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    card_json: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    news_through: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime] = mapped_column()
