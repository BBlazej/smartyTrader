"""Unit tests for the APScheduler wrapper."""

from __future__ import annotations

import asyncio
from typing import Any
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


class TestRescheduleCycle:
    """§7.50: an ``interval_minutes`` override must re-arm the live job, not just
    write to settings nobody reads."""

    def test_reschedules_with_new_interval(
        self, manager: AsyncSchedulerManager, mock_scheduler: MagicMock
    ) -> None:
        ok = manager.reschedule_cycle(12, job_id="crypto_cycle")

        assert ok is True
        mock_scheduler.reschedule_job.assert_called_once_with(
            "crypto_cycle", trigger="interval", minutes=12
        )

    def test_missing_job_is_fail_soft(
        self, manager: AsyncSchedulerManager, mock_scheduler: MagicMock
    ) -> None:
        mock_scheduler.reschedule_job.side_effect = RuntimeError("JobLookupError")

        assert manager.reschedule_cycle(12, job_id="crypto_cycle") is False  # never raises

    async def test_real_apscheduler_honours_the_rearmed_trigger(self) -> None:
        """Pin the real APScheduler contract behind :meth:`reschedule_cycle`."""
        from src.core.scheduler import create_async_scheduler

        scheduler = create_async_scheduler()
        scheduler.add_job(
            AsyncMock(),
            trigger="interval",
            minutes=5,
            id="crypto_cycle",
            max_instances=1,
            coalesce=True,
        )
        manager = AsyncSchedulerManager(scheduler)

        assert manager.reschedule_cycle(12, job_id="crypto_cycle") is True
        job = scheduler.get_job("crypto_cycle")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 12 * 60


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


class TestRealSchedulerSemantics:
    """§7.32: pin the APScheduler behaviors the wrapper's safety claims rest on.

    These run against a real ``AsyncIOScheduler`` on the test loop (no network,
    no mocks): overlap prevention (``max_instances=1``), misfire/coalesce
    collapsing, and survival of job errors — the exact policies
    :meth:`AsyncSchedulerManager.schedule_cycle` sets for trading cycles.
    """

    async def _run(self, scheduler: Any, seconds: float) -> None:
        scheduler.start()
        try:
            await asyncio.sleep(seconds)
        finally:
            scheduler.shutdown(wait=False)

    async def test_slow_job_never_overlaps_itself(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        sched = AsyncIOScheduler()
        active = 0
        max_active = 0
        completed = 0

        async def job() -> None:
            nonlocal active, max_active, completed
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.3)  # outlives several ticks
            active -= 1
            completed += 1

        # Same policy AsyncSchedulerManager pins: one instance, missed ticks collapsed.
        sched.add_job(job, "interval", seconds=0.05, id="cycle", max_instances=1, coalesce=True)
        await self._run(sched, 0.8)

        assert max_active == 1  # the guarantee: a slow cycle never overlaps
        assert completed >= 2  # and later ticks still execute after it finishes

    async def test_misfires_are_collapsed_not_replayed_per_tick(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        sched = AsyncIOScheduler()
        runs: list[float] = []
        started = asyncio.get_event_loop().time()

        async def job() -> None:
            runs.append(asyncio.get_event_loop().time() - started)
            if len(runs) == 1:
                await asyncio.sleep(0.6)  # block past many 0.05s ticks
            else:
                await asyncio.sleep(0.01)

        sched.add_job(job, "interval", seconds=0.05, id="cycle", max_instances=1, coalesce=True)
        await self._run(sched, 0.9)

        # ~18 ticks came due during the block; coalesce + max_instances must turn
        # them into a trickle of catch-ups, never one replay per missed tick.
        assert len(runs) <= 6

    async def test_job_errors_do_not_stop_later_ticks(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        sched = AsyncIOScheduler()
        calls: list[int] = []

        async def failing_job() -> None:
            calls.append(len(calls))
            raise RuntimeError("cycle boom")

        sched.add_job(
            failing_job, "interval", seconds=0.05, id="cycle", max_instances=1, coalesce=True
        )
        await self._run(sched, 0.35)

        assert len(calls) >= 2  # the scheduler survives exceptions and keeps ticking
        assert sched.get_job("cycle") is not None  # job not unscheduled by its own failure

    async def test_failing_job_does_not_disturb_the_other_job(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        sched = AsyncIOScheduler()
        pruned: list[int] = []

        async def boom() -> None:
            raise RuntimeError("cycle boom")

        async def keep_running() -> None:
            pruned.append(1)

        # Mirrors the runner's setup: trading cycle + storage prune on one scheduler.
        sched.add_job(
            boom, "interval", seconds=0.05, id="crypto_cycle", max_instances=1, coalesce=True
        )
        sched.add_job(
            keep_running,
            "interval",
            seconds=0.05,
            id="storage_prune",
            max_instances=1,
            coalesce=True,
        )
        await self._run(sched, 0.35)

        assert len(pruned) >= 2  # the healthy job keeps ticking next to a failing one

    def test_manager_policy_kwargs_are_the_overlap_guarantee(self) -> None:
        scheduler = MagicMock()
        manager = AsyncSchedulerManager(scheduler)
        manager.schedule_cycle(AsyncMock(), interval_minutes=5, job_id="crypto_cycle")
        kwargs = scheduler.add_job.call_args.kwargs
        assert kwargs["max_instances"] == 1
        assert kwargs["coalesce"] is True
