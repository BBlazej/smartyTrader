"""Unit tests for the APScheduler wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.scheduler import AsyncSchedulerManager


@pytest.fixture()
def mock_scheduler() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def manager(mock_scheduler: MagicMock) -> AsyncSchedulerManager:
    return AsyncSchedulerManager(mock_scheduler)


class TestScheduleCycle:
    def test_schedules_interval_job(
        self, manager: AsyncSchedulerManager, mock_scheduler: MagicMock
    ) -> None:
        job_func = AsyncMock()

        manager.schedule_cycle(job_func, interval_minutes=5, job_id="crypto_cycle")

        mock_scheduler.add_job.assert_called_once()
        args, kwargs = mock_scheduler.add_job.call_args
        assert args[0] is job_func
        assert kwargs["trigger"] == "interval"
        assert kwargs["minutes"] == 5
        assert kwargs["id"] == "crypto_cycle"
        assert kwargs["max_instances"] == 1
        assert kwargs["coalesce"] is True


class TestStart:
    def test_starts_scheduler(
        self, manager: AsyncSchedulerManager, mock_scheduler: MagicMock
    ) -> None:
        manager.start()
        mock_scheduler.start.assert_called_once()


class TestShutdown:
    def test_shuts_down_scheduler(
        self, manager: AsyncSchedulerManager, mock_scheduler: MagicMock
    ) -> None:
        manager.shutdown()
        mock_scheduler.shutdown.assert_called_once_with(wait=True)
