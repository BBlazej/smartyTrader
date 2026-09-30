"""Storage lifecycle: engine construction, WAL, identity, backup (§7.36).

``StorageBase`` owns the SQLite engines and the ``_session`` helper; the
:class:`src.core.storage.Storage` facade composes the data-access mixins on it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from sqlalchemy import ColumnElement, Delete, Select, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, DbIdentityRow

#: A statement the agent filter can narrow (a SELECT or a DELETE).
_Stmt = TypeVar("_Stmt", Select, Delete)


class DatabaseIdentityError(RuntimeError):
    """The database file holds another book (§7.78)."""


class StorageBase:
    """Async repository core: lifecycle + session factory for all data mixins.

    **Agent binding (§7.39).** Both agents share one SQLite file, so a runner builds
    ``Storage(path, agent="crypto")``: every write is stamped with that agent and every
    scoped read (decisions, orders, portfolio snapshots) filters on it — one agent can
    never rehydrate, seed its drawdown peak from, or feed its prompt with the other's
    rows. An *unbound* Storage (``agent=None`` — dashboard, backtest/prune CLIs, tests)
    reads across all agents unless a method is given an explicit ``agent=``.
    """

    def __init__(
        self,
        database_path: str,
        agent: str | None = None,
        identity: tuple[str, str] | None = None,
    ) -> None:
        # Normalize path — use absolute if relative
        db_path = Path(database_path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"sqlite+aiosqlite:///{db_path}"
        self._engine = create_async_engine(uri)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._closed = False
        self._agent = agent
        # ``(agent, mode)`` this file must hold (§7.78) — enforced by initialize();
        # ``None`` (dashboard, CLIs, tests) opens any file without checking.
        self._identity = identity
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

    def _where_agent(self, stmt: _Stmt, column: ColumnElement, agent: str | None = None) -> _Stmt:
        """``stmt`` filtered to the effective agent (§7.39); unbound + no ``agent`` → as is."""
        scope = self._agent_scope(agent)
        return stmt if scope is None else stmt.where(column == scope)

    @property
    def venue(self) -> str | None:
        """Execution venue stamped on new order/portfolio rows (``None`` = unstamped)."""
        return self._venue

    def bind_venue(self, venue: str | None) -> None:
        """Stamp subsequent order/portfolio writes with the executor's venue (§7.61)."""
        self._venue = venue

    @staticmethod
    def _venue_match(column: ColumnElement, venue: str) -> ColumnElement:
        """Rows of exactly ``venue`` (§7.61/§7.76) — every row is venue-stamped."""
        return column == venue

    def _risk_venue(self, venue: str | None) -> str | None:
        """The venue a risk seed reads (§7.76): explicit, else the bound one; ``None`` = all."""
        return venue if venue is not None else self._venue

    @staticmethod
    def _venue_applies(row_venue: str | None, venue: str | None) -> bool:
        """Python twin of :meth:`_venue_match` for single rows (reset rows)."""
        return venue is None or row_venue == venue

    @property
    def database_path(self) -> str:
        """Absolute path of the SQLite file (resolved at construction)."""
        return str(Path(self._engine.url.database).resolve())

    async def initialize(self) -> None:
        """Create missing tables (WAL on) and check the file's identity.

        ``create_all`` adds *missing tables* only — it never alters existing ones. No
        column migrations are carried: every book was current when they were removed
        (2026-09-30), so a future column change brings its own one-off migration.
        """
        # Use a sync engine just for schema work — AsyncEngine.run_sync() is not
        # available in all SQLAlchemy versions.
        db_path = str(Path(self._engine.url.database).resolve())
        sync_uri = f"sqlite:///{db_path}"
        sync_engine = create_engine(sync_uri)
        try:
            self._enable_wal(sync_engine)
            Base.metadata.create_all(sync_engine)
            if self._identity is not None:
                self._check_identity(sync_engine, self._identity)
        finally:
            sync_engine.dispose()

    def _check_identity(self, engine, identity: tuple[str, str]) -> None:
        """Stamp a new file with its ``(agent, mode)`` or refuse a foreign one (§7.78)."""
        agent, mode = identity
        with engine.begin() as conn:
            row = conn.execute(text("SELECT agent, mode FROM db_identity WHERE id = 1")).first()
            if row is not None:
                if (row[0], row[1]) != (agent, mode):
                    raise DatabaseIdentityError(
                        f"{self.database_path} holds the {row[1]} {row[0]} book, not "
                        f"{mode} {agent} — refusing to write into it"
                    )
                return
            conn.execute(
                text(
                    "INSERT INTO db_identity (id, agent, mode, created_at) "
                    "VALUES (1, :agent, :mode, :now)"
                ),
                # ISO text, as SQLAlchemy stores datetimes (sqlite3's default adapter is deprecated).
                {
                    "agent": agent,
                    "mode": mode,
                    "now": datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" "),
                },
            )

    async def get_identity(self) -> tuple[str, str] | None:
        """The file's recorded ``(agent, mode)``, or ``None`` (opened without an identity)."""
        async with await self._session() as session:
            row = await session.get(DbIdentityRow, 1)
            return (row.agent, row.mode) if row is not None else None

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
