"""Single-instance runner lock tests (§7.52).

Two runners of the same agent would share one SQLite DB while keeping separate
in-memory paper books, exit levels and pending-order state — so `run_agent` must refuse
the second one *before* touching anything.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import Settings
from src.core.runner import RunnerAlreadyRunning, RunnerLock, run_agent


def _settings(tmp_path: Path) -> Settings:
    config = tmp_path / "settings.yaml"
    db = tmp_path / "data" / "locked.db"
    config.write_text(
        f"""
llm: {{endpoint: "http://localhost:1234/v1/chat/completions", model: m}}
crypto_agent: {{enabled: true, interval_minutes: 5, pairs: ["BTC/USDT"], decision_history_limit: 10}}
stocks_agent: {{enabled: false, interval_minutes: 60, symbols: ["AAPL"], decision_history_limit: 10}}
risk: {{max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}}
storage: {{database_path: "{db}"}}
monitoring: {{log_level: INFO}}
"""
    )
    return Settings(str(config))


class _FakeAgent:
    def set_control_overrides_applier(self, applier) -> None:
        pass

    async def run_cycle(self):
        return []

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


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
        build_agent=lambda pipeline, storage, risk_engine, llm_client: _FakeAgent(),
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
    async def test_scheduled_run_refused_while_lock_held(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / "data" / "crypto.runner.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                build_agent=lambda pipeline, storage, risk_engine, llm_client: _FakeAgent(),
            )

        assert built == []  # nothing constructed…
        assert not (tmp_path / "data" / "locked.db").exists()  # …and the DB was never touched

    async def test_run_once_also_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / "data" / "crypto.runner.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(RunnerAlreadyRunning):
            await _run_once(settings, [])

    async def test_sequential_runs_succeed_lock_is_released(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        built: list[bool] = []

        # Patch what the runner would otherwise build against real services.
        with (
            patch("src.core.runner.DecisionPipeline"),
            patch("src.core.runner.rehydrate_from_storage", new=AsyncMock()),
            patch("src.core.runner.prune_storage", new=AsyncMock()),
        ):
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
            build_agent=lambda pipeline, storage, risk_engine, llm_client: _FakeAgent(),
        )
        assert built == []
        assert not (tmp_path / "data" / "crypto.runner.lock").exists()
