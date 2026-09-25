"""Storage lifecycle: engine construction, WAL, migrations, backup (§7.36).

``StorageBase`` owns the SQLite engines and the ``_session`` helper; the
:class:`src.core.storage.Storage` facade composes the data-access mixins on it.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import ColumnElement, create_engine, or_, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

#: Tables whose rows belong to one agent (§7.39); ``agent_control`` is keyed by agent
#: already and ``market_snapshots`` is a symbol-keyed candle cache.
_AGENT_SCOPED_TABLES: tuple[str, ...] = ("llm_decisions", "orders", "portfolio_snapshots")


class StorageBase:
    """Async repository core: lifecycle + session factory for all data mixins.

    **Agent binding (§7.39).** Both agents share one SQLite file, so a runner builds
    ``Storage(path, agent="crypto")``: every write is stamped with that agent and every
    scoped read (decisions, orders, portfolio snapshots) filters on it — one agent can
    never rehydrate, seed its drawdown peak from, or feed its prompt with the other's
    rows. An *unbound* Storage (``agent=None`` — dashboard, backtest/prune CLIs, tests)
    reads across all agents unless a method is given an explicit ``agent=``.
    """

    def __init__(self, database_path: str, agent: str | None = None) -> None:
        # Normalize path — use absolute if relative
        db_path = Path(database_path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"sqlite+aiosqlite:///{db_path}"
        self._engine = create_async_engine(uri)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._closed = False
        self._agent = agent
        # Execution venue stamped on order/portfolio rows (§7.61); bound by the runner
        # once the executor exists.
        self._venue: str | None = None

    @property
    def agent(self) -> str | None:
        """The agent this Storage is bound to (``None`` = unbound, all agents)."""
        return self._agent

    def _agent_scope(self, agent: str | None = None) -> str | None:
        """Effective agent for a call: an explicit ``agent`` wins, else the binding."""
        return agent if agent is not None else self._agent

    @property
    def venue(self) -> str | None:
        """Execution venue stamped on new order/portfolio rows (``None`` = unstamped)."""
        return self._venue

    def bind_venue(self, venue: str | None) -> None:
        """Stamp subsequent order/portfolio writes with the executor's venue (§7.61)."""
        self._venue = venue

    @staticmethod
    def _venue_match(column: ColumnElement, venue: str) -> ColumnElement:
        """Rows of ``venue`` plus legacy unstamped (NULL) rows (§7.61)."""
        return or_(column == venue, column.is_(None))

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
        # agent column (§7.39): added to the per-agent tables and backfilled — see
        # :meth:`_backfill_agent_column` for the attribution rules.
        for table in _AGENT_SCOPED_TABLES:
            if not inspector.has_table(table):
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            with engine.begin() as conn:
                if "agent" not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN agent VARCHAR(20) NULL"))
                    StorageBase._backfill_agent_column(conn, table)
                conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS ix_{table}_agent ON {table} (agent)")
                )
        # orders.created_at (§7.12): retention pruning needs a storage-time bound
        # for rows that never filled; backfill what we can from fills.
        if inspector.has_table("orders"):
            order_cols = {c["name"] for c in inspector.get_columns("orders")}
            if "realized_pnl" not in order_cols:  # §7.46 closing-fill outcomes
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE orders ADD COLUMN realized_pnl FLOAT NULL"))
            if "created_at" not in order_cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE orders ADD COLUMN created_at DATETIME NULL"))
                    conn.execute(
                        text("UPDATE orders SET created_at = filled_at WHERE created_at IS NULL")
                    )
            if "venue" not in order_cols:  # §7.61 — paper order ids are self-identifying
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE orders ADD COLUMN venue VARCHAR(40) NULL"))
                    conn.execute(
                        text("UPDATE orders SET venue = 'paper' WHERE order_id LIKE 'paper-%'")
                    )
        # Orphaned control rows (§7.59 L11): before find #16 the agents keyed their
        # latches/heartbeats as ``crypto_agent``/``stocks_agent``; the runners have used
        # ``crypto``/``stocks`` since §7.31, so those rows are read by nothing and only
        # show up as phantom agents. Exactly these two legacy names are removed.
        if inspector.has_table("agent_control"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "DELETE FROM agent_control WHERE agent IN ('crypto_agent', 'stocks_agent')"
                    )
                )
        # portfolio_snapshots.venue (§7.61): legacy rows stay NULL (read as any venue).
        if inspector.has_table("portfolio_snapshots"):
            snap_cols = {c["name"] for c in inspector.get_columns("portfolio_snapshots")}
            if "venue" not in snap_cols:
                with engine.begin() as conn:
                    conn.execute(
                        text("ALTER TABLE portfolio_snapshots ADD COLUMN venue VARCHAR(40) NULL")
                    )

    @staticmethod
    def _backfill_agent_column(conn, table: str) -> None:
        """Attribute pre-§7.39 rows to an agent (one-off, at the migration that adds the column).

        Decisions/orders carry a symbol: crypto pairs are ``BASE/QUOTE`` (always a
        slash), stock tickers never are. Portfolio snapshots only carry positions —
        a snapshot whose positions are *all* slash-free tickers is ``stocks``; every
        other one (including empty books, which are indistinguishable) is ``crypto``,
        the only agent enabled by default.
        """
        if table in ("llm_decisions", "orders"):
            conn.execute(
                text(
                    f"UPDATE {table} SET agent = CASE WHEN symbol LIKE '%/%' "
                    "THEN 'crypto' ELSE 'stocks' END WHERE agent IS NULL"
                )
            )
            return
        rows = conn.execute(
            text("SELECT id, positions_json FROM portfolio_snapshots WHERE agent IS NULL")
        ).fetchall()
        for row_id, positions_json in rows:
            try:
                symbols = [str(p.get("symbol", "")) for p in json.loads(positions_json or "[]")]
            except (ValueError, TypeError, AttributeError):
                symbols = []
            agent = "stocks" if symbols and all("/" not in s for s in symbols) else "crypto"
            conn.execute(
                text("UPDATE portfolio_snapshots SET agent = :agent WHERE id = :id"),
                {"agent": agent, "id": row_id},
            )

    async def close(self) -> None:
        await self._engine.dispose()
        self._closed = True

    async def backup(self, dest_path: str) -> None:
        """Point-in-time copy via SQLite's **online backup** API (§7.35).

        Consistent even while other connections write (WAL included), and cheap
        for this database's size. Used by the retention pass to snapshot the DB
        *before* pruning anything away.
        """
        import aiosqlite

        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        source = await aiosqlite.connect(self.database_path)
        target = await aiosqlite.connect(str(dest_path))
        try:
            await source.backup(target)
        finally:
            await target.close()
            await source.close()

    # ── Helpers ───────────────────────────────────────────────

    async def _session(self) -> AsyncSession:
        return self._session_factory()
