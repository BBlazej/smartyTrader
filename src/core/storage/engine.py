"""Storage lifecycle: engine construction, WAL, migrations, backup (§7.36).

``StorageBase`` owns the SQLite engines and the ``_session`` helper; the
:class:`src.core.storage.Storage` facade composes the data-access mixins on it.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


class StorageBase:
    """Async repository core: lifecycle + session factory for all data mixins."""

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
