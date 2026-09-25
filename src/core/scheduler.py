"""APScheduler wrapper for recurring agent cycles.

APScheduler's scheduler methods (``add_job``/``start``/``shutdown``) are
synchronous; only the *job functions* they run are async. APScheduler itself is
imported lazily in the factory so the rest of the package (and its tests) stays
importable without the dependency installed. Tests pass a ``MagicMock`` scheduler
that satisfies the :class:`Scheduler` protocol below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

logger = structlog.get_logger()

# A job is an async callable with no arguments (e.g. ``agent.run_cycle``).
JobFunc = Callable[[], Awaitable[Any]]


class Scheduler(Protocol):
    """The subset of APScheduler the agent relies on (all sync)."""

    def add_job(self, func: JobFunc, trigger: str, **kwargs: Any) -> Any: ...
    def reschedule_job(self, job_id: str, **kwargs: Any) -> Any: ...
    def start(self, wait: bool = False) -> None: ...
    def shutdown(self, wait: bool = True) -> None: ...


class AsyncSchedulerManager:
    """Thin, testable wrapper around an APScheduler ``AsyncIOScheduler``.

    Encapsulates: scheduling a recurring job, starting the loop, and graceful
    shutdown. ``max_instances=1`` + ``coalesce=True`` guarantee that a slow cycle
    can never overlap with the next one, and missed ticks are collapsed.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    def schedule_cycle(self, func: JobFunc, interval_minutes: int, job_id: str) -> Any:
        self._scheduler.add_job(
            func,
            trigger="interval",
            minutes=interval_minutes,
            id=job_id,
            max_instances=1,
            coalesce=True,
        )
        logger.info("scheduled job", job_id=job_id, interval_minutes=interval_minutes)

    def reschedule_cycle(self, interval_minutes: int, job_id: str) -> bool:
        """Re-arm an existing interval job with a new period (§7.50).

        Applied safe-config changes to ``interval_minutes`` must take effect without a
        restart — otherwise the override lands in settings and is silently ignored.
        Fail-soft: a missing job (or any scheduler hiccup) warns and returns False;
        the running loop keeps its current cadence rather than dying.
        """
        try:
            self._scheduler.reschedule_job(job_id, trigger="interval", minutes=interval_minutes)
        except Exception as exc:  # noqa: BLE001 - never kill the loop over a re-arm
            logger.warning(
                "could not reschedule job; keeping current cadence",
                job_id=job_id,
                interval_minutes=interval_minutes,
                error=str(exc),
            )
            return False
        logger.info("rescheduled job", job_id=job_id, interval_minutes=interval_minutes)
        return True

    def start(self) -> None:
        self._scheduler.start()
        logger.info("scheduler started")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=True)
        logger.info("scheduler stopped")


def create_async_scheduler() -> Any:
    """Build a real APScheduler ``AsyncIOScheduler`` (lazy import)."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # lazy import

    return AsyncIOScheduler()
