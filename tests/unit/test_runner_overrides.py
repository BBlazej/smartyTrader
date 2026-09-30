"""Runner-level safe-config override tests (§7.50).

The old behavior wrote ``interval_minutes`` into settings nobody re-read: the APScheduler
job kept its startup cadence forever, and stored overrides only landed after the first
cycle. These pin the fixed contract — stored overrides apply *before* scheduling, a live
change re-arms the job, and removal reverts to YAML.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core import db_layout
from src.core.config import Settings
from src.core.storage import Storage
from tests.helpers import StubAgent, make_settings, run_agent_once, runner_patches


def _settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


async def _seed_override(data_dir: str, raw: str | None) -> None:
    # The runner opens its own book file (§7.78): paper crypto in data_dir.
    book = db_layout.db_path(data_dir, "paper", "crypto")
    seed = Storage(str(book), identity=("crypto", "paper"))
    await seed.initialize()
    try:
        await seed.set_config_override("crypto", raw)
    finally:
        await seed.close()


async def _run(settings: Settings, agent: StubAgent, *, run_once: bool = False) -> None:
    with runner_patches():
        await run_agent_once(settings, agent, run_once=run_once)


class TestStartupOverrides:
    async def test_stored_interval_governs_scheduling(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        await _seed_override(settings.storage.data_dir, '{"interval_minutes": 7}')
        manager = MagicMock()
        fake_agent = StubAgent()

        task = asyncio.create_task(_scheduled_run(settings, fake_agent, manager))
        try:
            await asyncio.sleep(0.1)  # let startup + first cycle settle
            cycle_calls = [
                c
                for c in manager.schedule_cycle.call_args_list
                if c.kwargs.get("job_id") == "crypto_cycle"
            ]
            assert cycle_calls, "cycle job never scheduled"
            assert cycle_calls[0].args[1] == 7  # stored override, not YAML's 5
        finally:
            task.cancel()
            await task

    async def test_live_interval_change_rearms_the_job(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        manager = MagicMock()
        fake_agent = StubAgent()

        task = asyncio.create_task(_scheduled_run(settings, fake_agent, manager))
        try:
            await asyncio.sleep(0.1)
            assert fake_agent.applier is not None  # applier installed on the agent

            fake_agent.applier('{"interval_minutes": 12}')
            manager.reschedule_cycle.assert_called_once_with(12, job_id="crypto_cycle")
            assert settings.crypto_agent.interval_minutes == 12

            # §7.50 removal: reverting reverts settings AND the live cadence.
            fake_agent.applier(None)
            assert settings.crypto_agent.interval_minutes == 5
            manager.reschedule_cycle.assert_called_with(5, job_id="crypto_cycle")
        finally:
            task.cancel()
            await task

    async def test_run_once_applies_stored_overrides_without_a_scheduler(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        await _seed_override(settings.storage.data_dir, '{"pairs": ["ETH/EUR"]}')
        fake_agent = StubAgent()

        with patch("src.core.scheduler.AsyncSchedulerManager") as manager_cls:
            await _run(settings, fake_agent, run_once=True)

        assert settings.crypto_agent.pairs == ["ETH/EUR"]  # applied before the cycle
        assert fake_agent.cycles == 1
        manager_cls.assert_not_called()


async def _scheduled_run(settings: Settings, agent: StubAgent, manager: MagicMock) -> None:
    with (
        patch("src.core.scheduler.AsyncSchedulerManager", return_value=manager),
        patch("src.core.scheduler.create_async_scheduler"),
    ):
        await _run(settings, agent)
