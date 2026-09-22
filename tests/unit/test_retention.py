"""Tests for the shared retention-pruning wrapper used by both runners (§7.12)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.core.retention import prune_storage


def _settings(snapshot_days: int = 30, history_days: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_retention_days=snapshot_days,
        history_retention_days=history_days,
    )


class TestPruneStorage:
    async def test_passes_configured_windows_through(self) -> None:
        storage = MagicMock()
        storage.prune = AsyncMock(return_value={"market_snapshots": 7})

        counts = await prune_storage(storage, _settings(snapshot_days=30, history_days=90))

        assert counts == {"market_snapshots": 7}
        storage.prune.assert_awaited_once_with(snapshot_days=30, history_days=90)

    async def test_all_windows_disabled_skips_entirely(self) -> None:
        storage = MagicMock()
        storage.prune = AsyncMock()

        counts = await prune_storage(storage, _settings(snapshot_days=0, history_days=0))

        assert counts == {}
        storage.prune.assert_not_awaited()

    async def test_failure_is_fail_soft(self) -> None:
        """A broken DB must never take down the runner that scheduled the job."""
        storage = MagicMock()
        storage.prune = AsyncMock(side_effect=RuntimeError("database is locked"))

        counts = await prune_storage(storage, _settings())

        assert counts == {}  # swallowed with a warning, no raise


class TestDatabaseBackup:
    """§7.35: timestamped online-backup before every prune pass."""

    @staticmethod
    def _settings(snapshot_days: int, history_days: int, backup_dir: str, backup_keep: int = 0):
        return SimpleNamespace(
            snapshot_retention_days=snapshot_days,
            history_retention_days=history_days,
            backup_dir=backup_dir,
            backup_keep=backup_keep,
        )

    async def test_backup_covers_rows_the_prune_then_deletes(self, tmp_path) -> None:
        from datetime import UTC, datetime, timedelta

        from src.core.storage import Storage

        db = str(tmp_path / "agent.db")
        storage = Storage(db)
        await storage.initialize()
        try:
            old_id = await storage.save_market_snapshot("BTC/USDT", "5m", "[1]")
            cutoff_old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=40)
            async with await storage._session() as session:
                from sqlalchemy import text

                await session.execute(
                    text("UPDATE market_snapshots SET fetched_at = :old WHERE id = :rid"),
                    {"old": cutoff_old.strftime("%Y-%m-%d %H:%M:%S.%f"), "rid": old_id},
                )
                await session.commit()

            backups_dir = tmp_path / "backups"
            await prune_storage(
                storage,
                self._settings(snapshot_days=30, history_days=0, backup_dir=str(backups_dir)),
            )

            backups = sorted(backups_dir.glob("agent-*.db"))
            assert len(backups) == 1

            # The pruned row is gone from the live DB but recoverable from the backup.
            recent = await storage.get_recent_snapshots("BTC/USDT")
            assert old_id not in [s.id for s in recent]

            from sqlalchemy import create_engine
            from sqlalchemy import text as stext

            sync = create_engine(f"sqlite:///{backups[0]}")
            with sync.connect() as conn:
                n_backup = conn.execute(stext("SELECT COUNT(*) FROM market_snapshots")).scalar()
            sync.dispose()
            assert n_backup == 1
        finally:
            await storage.close()

    async def test_backup_disabled_writes_nothing(self, tmp_path) -> None:
        from src.core.storage import Storage

        storage = Storage(str(tmp_path / "agent.db"))
        await storage.initialize()
        try:
            await prune_storage(
                storage, self._settings(snapshot_days=30, history_days=0, backup_dir="")
            )
            assert not (tmp_path / "backups").exists()
        finally:
            await storage.close()

    async def test_rotation_keeps_only_the_newest(self, tmp_path) -> None:
        from src.core.storage import Storage

        storage = Storage(str(tmp_path / "agent.db"))
        await storage.initialize()
        try:
            backups_dir = tmp_path / "backups"
            for _ in range(3):
                await prune_storage(
                    storage,
                    self._settings(
                        snapshot_days=30, history_days=0, backup_dir=str(backups_dir), backup_keep=2
                    ),
                )
            remaining = sorted(backups_dir.glob("agent-*.db"))
            assert len(remaining) == 2
        finally:
            await storage.close()

    async def test_backup_failure_does_not_block_pruning(self) -> None:
        storage = MagicMock()
        storage.prune = AsyncMock(return_value={"market_snapshots": 3})
        storage.backup = AsyncMock(side_effect=RuntimeError("disk full"))
        settings = SimpleNamespace(
            snapshot_retention_days=30,
            history_retention_days=0,
            backup_dir="/somewhere",
            backup_keep=0,
        )

        counts = await prune_storage(storage, settings)

        assert counts == {"market_snapshots": 3}  # pruning ran despite backup failure
