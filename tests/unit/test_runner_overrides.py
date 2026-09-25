"""Runner-level safe-config override tests (§7.50).

The old behavior wrote ``interval_minutes`` into settings nobody re-read: the APScheduler
job kept its startup cadence forever, and stored overrides only landed after the first
cycle. These pin the fixed contract — stored overrides apply *before* scheduling, a live
change re-arms the job, and removal reverts to YAML.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import Settings
from src.core.runner import run_agent
from src.core.storage import Storage


def _settings(tmp_path: Path) -> Settings:
    config = tmp_path / "settings.yaml"
    config.write_text(
        f"""
llm: {{endpoint: "http://localhost:1234/v1/chat/completions", model: m}}
crypto_agent: {{enabled: true, interval_minutes: 5, pairs: ["BTC/USDT"], decision_history_limit: 10}}
stocks_agent: {{enabled: false, interval_minutes: 60, symbols: ["AAPL"], market_hours: "08:00-22:00", decision_history_limit: 10}}
risk: {{max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}}
storage: {{database_path: "{tmp_path / "runner.db"}"}}
monitoring: {{log_level: INFO}}
"""
    )
    return Settings(str(config))


async def _seed_override(db_path: str, raw: str | None) -> None:
    seed = Storage(db_path)
    await seed.initialize()
    try:
        await seed.set_config_override("crypto", raw)
    finally:
        await seed.close()


class _FakeAgent:
    """Minimal agent surface: captures the applier, counts cycles, cleans up."""

    def __init__(self) -> None:
        self.applier = None  # type: ignore[assignment]
        self.cycles = 0
        self.started = False
        self.shutdowns = 0

    def set_control_overrides_applier(self, applier) -> None:
        self.applier = applier

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = symbols

    async def run_cycle(self):
        self.cycles += 1
        return []

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shutdowns += 1


def _components() -> tuple[MagicMock, MagicMock]:
    provider = MagicMock()
    provider.close = AsyncMock()
    executor = MagicMock()
    executor.close = AsyncMock()
    return provider, executor


async def _run(settings: Settings, agent: _FakeAgent, *, run_once: bool = False) -> None:
    with (
        patch("src.core.runner.DecisionPipeline"),
        patch("src.core.runner.rehydrate_from_storage", new=AsyncMock()),
        patch("src.core.runner.prune_storage", new=AsyncMock()),
    ):
        await run_agent(
            settings,
            component="crypto",
            agent_enabled=True,
            interval_minutes=settings.crypto_agent.interval_minutes,
            decision_history_limit=settings.crypto_agent.decision_history_limit,
            job_id="crypto_cycle",
            build_components=_components,
            build_agent=lambda pipeline, storage, risk_engine, llm_client: agent,
            run_once=run_once,
        )


class TestStartupOverrides:
    async def test_stored_interval_governs_scheduling(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        await _seed_override(settings.storage.database_path, '{"interval_minutes": 7}')
        manager = MagicMock()
        fake_agent = _FakeAgent()

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
        fake_agent = _FakeAgent()

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
        await _seed_override(settings.storage.database_path, '{"pairs": ["ETH/USDT"]}')
        fake_agent = _FakeAgent()

        with patch("src.core.scheduler.AsyncSchedulerManager") as manager_cls:
            await _run(settings, fake_agent, run_once=True)

        assert settings.crypto_agent.pairs == ["ETH/USDT"]  # applied before the cycle
        assert fake_agent.cycles == 1
        manager_cls.assert_not_called()


async def _scheduled_run(settings: Settings, agent: _FakeAgent, manager: MagicMock) -> None:
    with (
        patch("src.core.runner.DecisionPipeline"),
        patch("src.core.runner.rehydrate_from_storage", new=AsyncMock()),
        patch("src.core.runner.prune_storage", new=AsyncMock()),
        patch("src.core.scheduler.AsyncSchedulerManager", return_value=manager),
        patch("src.core.scheduler.create_async_scheduler", return_value=MagicMock()),
    ):
        await run_agent(
            settings,
            component="crypto",
            agent_enabled=True,
            interval_minutes=settings.crypto_agent.interval_minutes,
            decision_history_limit=settings.crypto_agent.decision_history_limit,
            job_id="crypto_cycle",
            build_components=_components,
            build_agent=lambda pipeline, storage, risk_engine, llm_client: agent,
        )
