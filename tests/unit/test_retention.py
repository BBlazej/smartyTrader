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
