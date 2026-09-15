"""Tests for the stocks runner's enabled / --once semantics [R-H3].

``enabled: false`` must exit before constructing anything (no cycles, LLM
calls, order placement or DB writes); single-cycle mode is the explicit
``--once`` flag, never a side effect of disabling the agent.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.run_stocks_agent import run


def _run_settings(enabled: bool) -> SimpleNamespace:
    """A settings stub covering everything ``run()`` reads before the agent."""
    return SimpleNamespace(
        llm=SimpleNamespace(),
        stocks_agent=SimpleNamespace(
            enabled=enabled,
            symbols=["AAPL"],
            interval_minutes=15,
            market_hours="09:00-16:30",
            market_timezone="Europe/Warsaw",
            decision_history_limit=10,
        ),
        risk=SimpleNamespace(),
        execution=SimpleNamespace(paper_fee_pct=0.0, paper_slippage_pct=0.0),
        storage=SimpleNamespace(database_path=":memory:"),
        monitoring=SimpleNamespace(log_level="INFO", alert_dedup_window_seconds=300),
    )


class _FakeAgent:
    """Records lifecycle calls so tests can assert exactly what ran."""

    def __init__(self, fail_cycle: bool = False) -> None:
        self.cycles = 0
        self.starts = 0
        self.shutdowns = 0
        self._fail_cycle = fail_cycle

    async def start(self) -> None:
        self.starts += 1

    async def run_cycle(self) -> None:
        self.cycles += 1
        if self._fail_cycle:
            raise RuntimeError("cycle boom")

    async def shutdown(self) -> None:
        self.shutdowns += 1


def _mock_env() -> tuple[MagicMock, MagicMock, AsyncMock]:
    """Provider/executor/storage stand-ins with awaitable closes."""
    provider = MagicMock()
    provider.close = AsyncMock()
    executor = MagicMock()
    executor.close = AsyncMock()
    storage = AsyncMock()
    return provider, executor, storage


class TestEnabledSemantics:
    async def test_disabled_exits_before_constructing_anything(self) -> None:
        settings = _run_settings(enabled=False)
        with (
            patch.dict(os.environ, {}, clear=True),  # no XTB_API_KEY either way
            patch("scripts.run_stocks_agent.Settings", return_value=settings),
            patch("scripts.run_stocks_agent.setup_logging"),
            patch("scripts.run_stocks_agent.Storage") as mock_storage,
            patch("scripts.run_stocks_agent.LLMClient") as mock_llm,
            patch("scripts.run_stocks_agent.create_xtb_provider") as mock_provider_cls,
            patch("scripts.run_stocks_agent.PaperExecutor") as mock_executor_cls,
            patch("scripts.run_stocks_agent.DecisionPipeline") as mock_pipeline,
            patch("scripts.run_stocks_agent.StocksAgent") as mock_agent_cls,
        ):
            await run()

        # A disabled agent must not touch the DB, the LLM, the provider/executor,
        # the pipeline or the agent itself — no cycles, orders or writes.
        mock_storage.assert_not_called()
        mock_llm.assert_not_called()
        mock_provider_cls.assert_not_called()
        mock_executor_cls.assert_not_called()
        mock_pipeline.assert_not_called()
        mock_agent_cls.assert_not_called()

    async def test_run_once_runs_exactly_one_cycle_then_shuts_down(self) -> None:
        settings = _run_settings(enabled=True)
        fake_agent = _FakeAgent()
        provider, executor, storage = _mock_env()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("scripts.run_stocks_agent.Settings", return_value=settings),
            patch("scripts.run_stocks_agent.setup_logging"),
            patch("scripts.run_stocks_agent.Storage", return_value=storage),
            patch("scripts.run_stocks_agent.LLMClient", return_value=MagicMock()),
            patch("scripts.run_stocks_agent.RiskEngine"),
            patch("scripts.run_stocks_agent.create_xtb_provider", return_value=provider),
            patch("scripts.run_stocks_agent.PaperExecutor", return_value=executor),
            patch("scripts.run_stocks_agent.DecisionPipeline"),
            patch("scripts.run_stocks_agent.StocksAgent", return_value=fake_agent),
            patch("src.core.scheduler.AsyncSchedulerManager") as mock_manager_cls,
        ):
            await run(run_once=True)

        assert fake_agent.cycles == 1
        assert fake_agent.shutdowns == 1
        provider.close.assert_awaited_once()
        executor.close.assert_awaited_once()
        storage.close.assert_awaited_once()
        # Single-cycle mode never starts the scheduler.
        mock_manager_cls.assert_not_called()

    async def test_run_once_failure_still_releases_resources(self) -> None:
        settings = _run_settings(enabled=True)
        fake_agent = _FakeAgent(fail_cycle=True)
        provider, executor, storage = _mock_env()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("scripts.run_stocks_agent.Settings", return_value=settings),
            patch("scripts.run_stocks_agent.setup_logging"),
            patch("scripts.run_stocks_agent.Storage", return_value=storage),
            patch("scripts.run_stocks_agent.LLMClient", return_value=MagicMock()),
            patch("scripts.run_stocks_agent.RiskEngine"),
            patch("scripts.run_stocks_agent.create_xtb_provider", return_value=provider),
            patch("scripts.run_stocks_agent.PaperExecutor", return_value=executor),
            patch("scripts.run_stocks_agent.DecisionPipeline"),
            patch("scripts.run_stocks_agent.StocksAgent", return_value=fake_agent),
            pytest.raises(RuntimeError, match="cycle boom"),
        ):
            await run(run_once=True)

        # A failed --once cycle propagates (non-zero exit) but still cleans up.
        provider.close.assert_awaited_once()
        executor.close.assert_awaited_once()
        storage.close.assert_awaited_once()
