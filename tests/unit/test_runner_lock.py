"""Single-instance runner lock tests (§7.52).

Two runners of the same agent would share one SQLite DB while keeping separate
in-memory paper books, exit levels and pending-order state — so `run_agent` must refuse
the second one *before* touching anything.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import Settings
from src.core.runner import RunnerAlreadyRunning, RunnerLock, run_agent
from tests.helpers import StubAgent, make_settings, runner_patches


def _settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, {"storage": {"data_dir": str(tmp_path / "data")}})


def _components(built: list[bool]) -> object:
    def build() -> tuple[MagicMock, MagicMock]:
        built.append(True)
        provider = MagicMock()
        provider.close = AsyncMock()
        executor = MagicMock()
        executor.close = AsyncMock()
        return provider, executor

    return build


async def _run_once(settings: Settings, built: list[bool]) -> None:
    await run_agent(
        settings,
        component="crypto",
        agent_enabled=True,
        interval_minutes=5,
        decision_history_limit=10,
        job_id="crypto_cycle",
        build_components=_components(built),
        build_agent=lambda pipeline, storage, risk_engine, llm_client: StubAgent(),
        run_once=True,
    )


class TestRunnerLock:
    def test_acquire_writes_pid_and_blocks_second_handle(self, tmp_path: Path) -> None:
        path = tmp_path / "crypto.runner.lock"
        first = RunnerLock(path)
        assert first.acquire() is True
        assert path.read_text() == str(os.getpid())

        second = RunnerLock(path)
        assert second.acquire() is False  # separate handle → real contention

        first.release()
        third = RunnerLock(path)
        assert third.acquire() is True  # released cleanly
        third.release()

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        lock = RunnerLock(tmp_path / "a.lock")
        lock.release()  # never acquired → no-op
        assert lock.acquire() is True
        lock.release()
        lock.release()


class TestRunAgentRefusesDoubleStart:
    async def test_paper_and_demo_of_one_agent_may_run_side_by_side(
        self, tmp_path: Path, request: pytest.FixtureRequest
    ) -> None:
        # §7.78: another mode's lock doesn't block this one — separate files.
        settings = _settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / "data" / "demo_crypto.runner.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        request.addfinalizer(holder.close)
        built: list[bool] = []
        with runner_patches():
            await _run_once(settings, built)  # paper runs while demo holds its lock
        assert built == [True]
        assert (tmp_path / "data" / "paper_crypto.db").exists()

    async def test_scheduled_run_refused_while_lock_held(
        self, tmp_path: Path, request: pytest.FixtureRequest
    ) -> None:
        settings = _settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / "data" / "paper_crypto.runner.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        request.addfinalizer(holder.close)
        built: list[bool] = []

        with pytest.raises(RunnerAlreadyRunning):
            await run_agent(
                settings,
                component="crypto",
                agent_enabled=True,
                interval_minutes=5,
                decision_history_limit=10,
                job_id="crypto_cycle",
                build_components=_components(built),
                build_agent=lambda pipeline, storage, risk_engine, llm_client: StubAgent(),
            )

        # §7.78: the executor decides the mode (and so the lock), so the components
        # are built — no I/O — and closed again; the DB is never touched.
        assert built == [True]
        assert not (tmp_path / "data" / "paper_crypto.db").exists()
        assert not (tmp_path / "data" / "locked.db").exists()

    async def test_run_once_also_refused(
        self, tmp_path: Path, request: pytest.FixtureRequest
    ) -> None:
        settings = _settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / "data" / "paper_crypto.runner.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        request.addfinalizer(holder.close)

        with pytest.raises(RunnerAlreadyRunning):
            await _run_once(settings, [])

    async def test_sequential_runs_succeed_lock_is_released(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        built: list[bool] = []

        # Patch what the runner would otherwise build against real services.
        with runner_patches():
            await _run_once(settings, built)
            await _run_once(settings, built)  # same process, after graceful release

        assert built == [True, True]

    async def test_disabled_agent_never_touches_the_lock(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        built: list[bool] = []
        await run_agent(
            settings,
            component="crypto",
            agent_enabled=False,  # enabled-gate keeps precedence
            interval_minutes=5,
            decision_history_limit=10,
            job_id="crypto_cycle",
            build_components=_components(built),
            build_agent=lambda pipeline, storage, risk_engine, llm_client: StubAgent(),
        )
        assert built == []
        assert not (tmp_path / "data" / "paper_crypto.runner.lock").exists()


async def test_sigterm_cancels_the_main_task_for_a_clean_shutdown() -> None:
    # §7.89: SIGTERM (dashboard Stop, docker stop) runs the same cleanup as Ctrl+C —
    # Python's default would kill the process mid-generation without any of it.
    import asyncio
    import signal

    from src.core.runner import _cancel_on_sigterm

    cleaned_up = asyncio.Event()

    async def main() -> None:
        _cancel_on_sigterm()
        try:
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(5)
        finally:
            cleaned_up.set()

    task = asyncio.create_task(main())
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
    assert cleaned_up.is_set()
