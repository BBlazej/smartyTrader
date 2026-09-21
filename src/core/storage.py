"""SQLite persistence layer via SQLAlchemy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import (
    Float,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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


class PortfolioSnapshotRow(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    cash: Mapped[float] = mapped_column(Float)
    positions_json: Mapped[str] = mapped_column(Text, default="[]")
    total_value: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


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


# ── Repository ────────────────────────────────────────────────


class Storage:
    """Async repository for all trading data."""

    def __init__(self, database_path: str) -> None:
        # Normalize path — use absolute if relative
        db_path = Path(database_path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"sqlite+aiosqlite:///{db_path}"
        self._engine = create_async_engine(uri)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._closed = False

    @property
    def database_path(self) -> str:
        """Absolute path of the SQLite file (resolved at construction)."""
        return str(Path(self._engine.url.database).resolve())

    async def initialize(self) -> None:
        """Create tables if they don't exist and apply lightweight migrations.

        ``create_all`` only adds *missing tables* — it never alters existing ones —
        so a database created before a new column was added is migrated here with
        an idempotent ``ALTER TABLE ADD COLUMN`` (guarded by a column check).
        """
        # Use a sync engine just for schema work — AsyncEngine.run_sync() is not
        # available in all SQLAlchemy versions.
        db_path = str(Path(self._engine.url.database).resolve())
        sync_uri = f"sqlite:///{db_path}"
        sync_engine = create_engine(sync_uri)
        try:
            self._enable_wal(sync_engine)
            Base.metadata.create_all(sync_engine)
            self._apply_migrations(sync_engine)
        finally:
            sync_engine.dispose()

    @staticmethod
    def _enable_wal(engine) -> None:
        """Switch the database to Write-Ahead Logging (WAL) mode.

        WAL lets a reader (dashboard, backtester) proceed while the agent writes,
        avoiding ``database is locked`` errors when concurrent agents run. The mode
        is a persistent property of the SQLite file, so setting it here (once per
        startup) covers every subsequent connection, including the async engine.
        """
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))

    @staticmethod
    def _apply_migrations(engine) -> None:
        """Add columns introduced after a database file was first created.

        SQLite ``ALTER TABLE ADD COLUMN`` is cheap and idempotent here (we only
        add a column that is not already present), so this is safe to run on
        every startup.
        """
        from sqlalchemy import inspect, text

        inspector = inspect(engine)
        if inspector.has_table("llm_decisions"):
            existing = {c["name"] for c in inspector.get_columns("llm_decisions")}
            if "realized_pnl" not in existing:
                with engine.begin() as conn:
                    conn.execute(
                        text("ALTER TABLE llm_decisions ADD COLUMN realized_pnl FLOAT NULL")
                    )
            if "is_fallback" not in existing:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE llm_decisions "
                            "ADD COLUMN is_fallback INTEGER NOT NULL DEFAULT 0"
                        )
                    )
        # orders.created_at (§7.12): retention pruning needs a storage-time bound
        # for rows that never filled; backfill what we can from fills.
        if inspector.has_table("orders"):
            order_cols = {c["name"] for c in inspector.get_columns("orders")}
            if "created_at" not in order_cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE orders ADD COLUMN created_at DATETIME NULL"))
                    conn.execute(
                        text("UPDATE orders SET created_at = filled_at WHERE created_at IS NULL")
                    )

    async def close(self) -> None:
        await self._engine.dispose()
        self._closed = True

    # ── Helpers ───────────────────────────────────────────────

    async def _session(self) -> AsyncSession:
        return self._session_factory()

    # ── Market Snapshots ──────────────────────────────────────

    async def save_market_snapshot(
        self,
        symbol: str,
        timeframe: str,
        candles_json: str,
        indicators_json: str = "{}",
    ) -> int:
        async with await self._session() as session:
            row = MarketSnapshotRow(
                symbol=symbol,
                timeframe=timeframe,
                candles_json=candles_json,
                indicators_json=indicators_json,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_recent_snapshots(
        self,
        symbol: str,
        limit: int = 10,
    ) -> list[MarketSnapshotRow]:
        async with await self._session() as session:
            stmt = (
                select(MarketSnapshotRow)
                .where(MarketSnapshotRow.symbol == symbol)
                .order_by(MarketSnapshotRow.fetched_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ── LLM Decisions ─────────────────────────────────────────

    async def save_llm_decision(
        self,
        symbol: str,
        action: str,
        confidence: float,
        reasoning: str,
        stop_loss: float | None,
        take_profit: float | None,
        risk_verdict: str,
        risk_reason: str | None,
        realized_pnl: float | None = None,
        is_fallback: bool = False,
    ) -> int:
        async with await self._session() as session:
            row = LLMDecisionRow(
                symbol=symbol,
                action=action,
                confidence=confidence,
                reasoning=reasoning,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_verdict=risk_verdict,
                risk_reason=risk_reason,
                realized_pnl=realized_pnl,
                is_fallback=is_fallback,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def set_realized_pnl(self, decision_id: int, realized_pnl: float) -> None:
        """Stamp the net realized PnL onto a decision once its position closed.

        Called by the agent when an order realizes PnL, so the decision row
        carries the *outcome* the LLM is later shown (the "learn from its track
        record" loop). Fails soft — an outcome-recording error must not break
        a trading cycle.
        """
        from sqlalchemy import update

        log = structlog.get_logger()
        try:
            async with await self._session() as session:
                await session.execute(
                    update(LLMDecisionRow)
                    .where(LLMDecisionRow.id == decision_id)
                    .values(realized_pnl=realized_pnl)
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to record realized pnl", decision_id=decision_id, error=str(exc))

    async def add_realized_pnl(self, decision_id: int, delta: float) -> None:
        """Accumulate realized PnL onto a (typically the *entry*) decision (§7.8).

        A position closed in several tranches must sum its shares onto the same
        opening decision; unlike :meth:`set_realized_pnl` this adds to any
        existing value instead of overwriting. Fail-soft like ``set_realized_pnl``.
        """
        from sqlalchemy import func, update

        log = structlog.get_logger()
        try:
            async with await self._session() as session:
                await session.execute(
                    update(LLMDecisionRow)
                    .where(LLMDecisionRow.id == decision_id)
                    .values(realized_pnl=func.coalesce(LLMDecisionRow.realized_pnl, 0.0) + delta)
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to accumulate realized pnl", decision_id=decision_id, error=str(exc)
            )

    async def get_closed_decisions(self, limit: int = 50) -> list[LLMDecisionRow]:
        """Most recent decisions that carry a realized outcome (``realized_pnl`` set).

        Used at startup to rehydrate the consecutive-loss/cooldown trackers (§7.7).
        """
        async with await self._session() as session:
            stmt = (
                select(LLMDecisionRow)
                .where(LLMDecisionRow.realized_pnl.isnot(None))
                .order_by(LLMDecisionRow.timestamp.desc(), LLMDecisionRow.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_recent_decisions(
        self,
        symbol: str | None = None,
        limit: int = 10,
        include_fallback: bool = False,
    ) -> list[LLMDecisionRow]:
        """Return the most recent decisions, most-recent first.

        Each row now carries its ``realized_pnl`` outcome (None while the
        position is still open), which is surfaced to the LLM as context.
        ``include_fallback=True`` widens the view for audit surfaces (control API /
        dashboard); prompt context keeps using the default that excludes them (§7.8).
        """
        async with await self._session() as session:
            # LLM-unavailable fallback rows are audit-only context — never re-fed
            # to the model as if it had genuinely decided to HOLD (§7.8).
            stmt = select(LLMDecisionRow)
            if not include_fallback:
                stmt = stmt.where(LLMDecisionRow.is_fallback.isnot(True))
            stmt = stmt.order_by(LLMDecisionRow.timestamp.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(LLMDecisionRow.symbol == symbol)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_decisions_in_range(
        self,
        start: datetime,
        end: datetime,
        symbols: list[str] | None = None,
        include_fallback: bool = False,
    ) -> list[LLMDecisionRow]:
        """Decisions within ``[start, end]`` (UTC), oldest first (§7.14 replay input).

        LLM-fallback rows are excluded by default — they never produced a real
        trade decision (§7.8) and would only add noise to the replay.
        Timestamps are compared as naive UTC (SQLite has no tz-aware storage).
        """
        cutoff_start = _as_naive_utc(start)
        cutoff_end = _as_naive_utc(end)
        async with await self._session() as session:
            stmt = select(LLMDecisionRow).where(
                LLMDecisionRow.timestamp >= cutoff_start,
                LLMDecisionRow.timestamp <= cutoff_end,
            )
            if symbols:
                stmt = stmt.where(LLMDecisionRow.symbol.in_(symbols))
            if not include_fallback:
                stmt = stmt.where(LLMDecisionRow.is_fallback == False)
            stmt = stmt.order_by(LLMDecisionRow.timestamp.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ── Orders ────────────────────────────────────────────────

    async def save_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float | None,
        status: str,
        decision_id: int | None = None,
        filled_at: datetime | None = None,
    ) -> int:
        async with await self._session() as session:
            row = OrderRow(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status=status,
                decision_id=decision_id,
                filled_at=filled_at,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_recent_orders(
        self,
        symbol: str | None = None,
        limit: int = 20,
    ) -> list[OrderRow]:
        async with await self._session() as session:
            stmt = select(OrderRow).order_by(OrderRow.id.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(OrderRow.symbol == symbol)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_filled_orders(self, symbol: str | None = None) -> list[OrderRow]:
        """All *filled* orders in chronological order (insertion order).

        Used at startup to replay the FIFO lot ledger into executors (§7.25):
        rows carry ``side``, ``quantity``, ``price`` and the originating
        ``decision_id``. Ids are monotonic with execution time for both paper
        and venue paths (rows are written when the fill happens).
        """
        async with await self._session() as session:
            stmt = select(OrderRow).where(OrderRow.status == "filled").order_by(OrderRow.id.asc())
            if symbol:
                stmt = stmt.where(OrderRow.symbol == symbol)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ── Portfolio Snapshots ───────────────────────────────────

    async def save_portfolio_snapshot(
        self,
        cash: float,
        positions_json: str,
        total_value: float,
        unrealized_pnl: float = 0.0,
    ) -> int:
        async with await self._session() as session:
            row = PortfolioSnapshotRow(
                cash=cash,
                positions_json=positions_json,
                total_value=total_value,
                unrealized_pnl=unrealized_pnl,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_first_portfolio_snapshot_of_day(self) -> PortfolioSnapshotRow | None:
        """Earliest portfolio snapshot of the current UTC day (or ``None``).

        Its ``total_value`` rehydrates today's daily-loss baseline after a
        restart (§7.7). Timestamps are stored as naive UTC.
        """
        from datetime import time as dtime

        day_start = datetime.combine(datetime.now(UTC).date(), dtime.min)
        async with await self._session() as session:
            stmt = (
                select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.timestamp >= day_start)
                .order_by(PortfolioSnapshotRow.timestamp.asc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_max_portfolio_value(self) -> float | None:
        """Highest total_value ever recorded in portfolio snapshots (or ``None``).

        Used to seed the risk engine's drawdown high-water mark at startup so
        the guard persists across process restarts.
        """
        from sqlalchemy import func

        async with await self._session() as session:
            result = await session.execute(select(func.max(PortfolioSnapshotRow.total_value)))
            value = result.scalar()
            return float(value) if value is not None else None

    async def get_latest_portfolio_snapshot(self) -> PortfolioSnapshotRow | None:
        async with await self._session() as session:
            stmt = select(PortfolioSnapshotRow).order_by(PortfolioSnapshotRow.id.desc()).limit(1)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_portfolio_history(self, limit: int = 100) -> list[PortfolioSnapshotRow]:
        async with await self._session() as session:
            stmt = (
                select(PortfolioSnapshotRow).order_by(PortfolioSnapshotRow.id.desc()).limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ── Agent control plane (§7.15) ───────────────────────

    async def get_agent_control(self, agent: str) -> AgentControlRow | None:
        """Fetch the control row for ``agent`` (``None`` = never touched, use defaults)."""
        async with await self._session() as session:
            return await session.get(AgentControlRow, agent)

    async def _upsert_agent_control(self, agent: str, **values: object) -> AgentControlRow:
        """Create the control row on first write, then patch the given columns."""
        async with await self._session() as session:
            row = await session.get(AgentControlRow, agent)
            if row is None:
                row = AgentControlRow(agent=agent)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()
            return row

    async def set_agent_state(self, agent: str, state: str) -> AgentControlRow:
        """Pause/resume an agent. The agent picks the change up on its next cycle."""
        if state not in ("running", "paused"):
            raise ValueError(f"invalid agent state: {state!r}")
        return await self._upsert_agent_control(agent, state=state)

    async def request_close_all(self, agent: str, requested: bool = True) -> AgentControlRow:
        """Set/clear the close-all latch; the agent closes every position then clears it."""
        return await self._upsert_agent_control(agent, close_all_requested=requested)

    async def record_cycle_health(
        self, agent: str, last_error: str | None = None
    ) -> AgentControlRow:
        """Heartbeat after a cycle: stamp ``last_cycle_at`` and the latest error."""
        return await self._upsert_agent_control(
            agent,
            last_cycle_at=datetime.now(UTC).replace(tzinfo=None),
            last_error=last_error,
        )

    async def set_config_override(self, agent: str, overrides_json: str | None) -> AgentControlRow:
        """Persist the whitelist-validated safe-config overrides (``None`` clears them)."""
        return await self._upsert_agent_control(agent, config_override_json=overrides_json)

    # ── Retention (§7.12) ─────────────────────────────────────

    async def prune(self, snapshot_days: int, history_days: int = 0) -> dict[str, int]:
        """Delete rows older than the retention windows; returns ``{table: deleted}``.

        - ``market_snapshots`` older than ``snapshot_days`` — the space hogs (~100-candle
          JSON per symbol-cycle). They are re-creatable cache: the backtester pulls
          fresh candles rather than replaying stored snapshots.
        - ``llm_decisions`` + ``orders`` older than ``history_days`` — the trade record
          (audit trail, fine-tuning dataset, and cooldown rehydration walks closed
          decisions), so this window is opt-in: ``0`` keeps everything.
        - ``portfolio_snapshots`` are **never pruned**: the drawdown high-water seed
          reads MAX over their full history, and pruning them would silently weaken
          that guard after a restart. They are also tiny (no candle blobs).

        A window of ``<= 0`` disables deletion for it; both disabled ⇒ no-op.
        Timestamps compare as naive UTC (SQLite has no tz-aware storage).
        """
        counts: dict[str, int] = {}
        if snapshot_days <= 0 and history_days <= 0:
            return counts
        now = datetime.now(UTC).replace(tzinfo=None)
        async with await self._session() as session:
            if snapshot_days > 0:
                cutoff = now - timedelta(days=snapshot_days)
                result = await session.execute(
                    delete(MarketSnapshotRow).where(MarketSnapshotRow.fetched_at < cutoff)
                )
                counts["market_snapshots"] = result.rowcount or 0
            if history_days > 0:
                cutoff = now - timedelta(days=history_days)
                result = await session.execute(
                    delete(LLMDecisionRow).where(LLMDecisionRow.timestamp < cutoff)
                )
                counts["llm_decisions"] = result.rowcount or 0
                result = await session.execute(delete(OrderRow).where(OrderRow.created_at < cutoff))
                counts["orders"] = result.rowcount or 0
            await session.commit()
        return counts
