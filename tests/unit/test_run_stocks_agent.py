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


def _storage_mock() -> AsyncMock:
    """Async Storage stand-in; ``bind_venue`` is the one sync method (§7.61)."""
    storage = AsyncMock()
    storage.bind_venue = MagicMock()
    return storage


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
            market_holidays=[],
            decision_history_limit=10,
            timeframe="1d",
            decide_on_new_bar_only=True,
        ),
        risk=SimpleNamespace(),
        execution=SimpleNamespace(
            paper_fee_pct=0.0, paper_slippage_pct=0.0, initial_cash=100_000.0
        ),
        # Retention fields (§7.12): windows off here so lifecycle tests stay
        # focused; pruning itself is covered in test_storage/test_retention.
        storage=SimpleNamespace(
            database_path=":memory:",
            snapshot_retention_days=0,
            history_retention_days=0,
            prune_interval_minutes=0,
        ),
        monitoring=SimpleNamespace(log_level="INFO", alert_dedup_window_seconds=300),
        # §7.16: paper stays the executor in lifecycle tests unless a test opts in.
        xtb_execution=SimpleNamespace(
            enabled=False,
            host="wss://ws.xapi.pro",
            account_type="demo",
            request_timeout_seconds=10.0,
        ),
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
    storage = _storage_mock()
    return provider, executor, storage


class TestEnabledSemantics:
    async def test_disabled_exits_before_constructing_anything(self) -> None:
        settings = _run_settings(enabled=False)
        with (
            patch.dict(os.environ, {}, clear=True),  # no XTB_API_KEY either way
            patch("scripts.run_stocks_agent.Settings", return_value=settings),
            patch("scripts.run_stocks_agent.setup_logging"),
            patch("src.core.runner.Storage") as mock_storage,
            patch("src.core.runner.LLMClient") as mock_llm,
            patch("scripts.run_stocks_agent.create_xtb_provider") as mock_provider_cls,
            patch("scripts.run_stocks_agent.PaperExecutor") as mock_executor_cls,
            patch("src.core.runner.DecisionPipeline") as mock_pipeline,
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
            patch("src.core.runner.Storage", return_value=storage) as storage_cls,
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch("scripts.run_stocks_agent.create_xtb_provider", return_value=provider),
            patch("scripts.run_stocks_agent.PaperExecutor", return_value=executor),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_stocks_agent.StocksAgent", return_value=fake_agent),
            patch("src.core.scheduler.AsyncSchedulerManager") as mock_manager_cls,
        ):
            await run(run_once=True)

        assert fake_agent.cycles == 1
        assert fake_agent.shutdowns == 1
        provider.close.assert_awaited_once()
        executor.close.assert_awaited_once()
        storage.close.assert_awaited_once()
        # Agent-bound storage (§7.39): every row this runner writes/reads is its own.
        assert storage_cls.call_args.kwargs["agent"] == "stocks"
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
            patch("src.core.runner.Storage", return_value=storage),
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch("scripts.run_stocks_agent.create_xtb_provider", return_value=provider),
            patch("scripts.run_stocks_agent.PaperExecutor", return_value=executor),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_stocks_agent.StocksAgent", return_value=fake_agent),
            pytest.raises(RuntimeError, match="cycle boom"),
        ):
            await run(run_once=True)

        # A failed --once cycle propagates (non-zero exit) but still cleans up.
        provider.close.assert_awaited_once()
        executor.close.assert_awaited_once()
        storage.close.assert_awaited_once()


class TestYFinanceFailFast:
    """A missing ``yfinance`` must fail fast with an actionable hint [§7.3]."""

    async def test_missing_yfinance_exits_with_install_hint(self) -> None:
        settings = _run_settings(enabled=True)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("scripts.run_stocks_agent.Settings", return_value=settings),
            patch("scripts.run_stocks_agent.setup_logging"),
            patch("src.core.runner.Storage", return_value=_storage_mock()),
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch(
                "scripts.run_stocks_agent.create_xtb_provider",
                side_effect=ImportError("No module named 'yfinance'"),
            ),
            pytest.raises(SystemExit, match="yfinance"),
        ):
            await run(run_once=True)


class TestExecutorSelection:
    """§7.16: XTB demo execution is opt-in — config AND env credentials, else paper."""

    async def _run_with(self, env: dict[str, str], *, xtb_enabled: bool):
        settings = _run_settings(enabled=True)
        settings.xtb_execution.enabled = xtb_enabled
        provider, executor, storage = _mock_env()
        xtb_executor_inst = MagicMock()
        xtb_executor_inst.close = AsyncMock()

        patches = [
            patch("scripts.run_stocks_agent.Settings", return_value=settings),
            patch("scripts.run_stocks_agent.setup_logging"),
            patch("src.core.runner.Storage", return_value=storage),
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch("scripts.run_stocks_agent.create_xtb_provider", return_value=provider),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_stocks_agent.StocksAgent", return_value=_FakeAgent()),
        ]
        client_patch = patch("scripts.run_stocks_agent.XApiClient")
        executor_patch = patch(
            "scripts.run_stocks_agent.XTBExecutor", return_value=xtb_executor_inst
        )
        paper_patch = patch("scripts.run_stocks_agent.PaperExecutor", return_value=executor)

        with patch.dict(os.environ, env, clear=True):
            started = [p.start() for p in patches + [client_patch, executor_patch, paper_patch]]
            try:
                await run(run_once=True)
            finally:
                for p in patches + [client_patch, executor_patch, paper_patch]:
                    p.stop()
        # start() returns each mock: ..., XApiClient, XTBExecutor, PaperExecutor.
        return started[-3], started[-1], started[-2]

    async def test_enabled_with_credentials_wires_xtb_executor(self) -> None:
        xtb_client, paper, xtb_executor = await self._run_with(
            {
                "XTB_ACCOUNT_ID": "42",
                "XTB_ACCOUNT_PASSWORD": "v3r1fy",
            },
            xtb_enabled=True,
        )
        xtb_client.assert_called_once()
        assert xtb_client.call_args.kwargs["account_type"] == "demo"
        xtb_executor.assert_called_once_with(xtb_client.return_value, venue="xtb-demo")
        paper.assert_not_called()

    async def test_enabled_without_credentials_stays_on_paper(self) -> None:
        xtb_client, paper, xtb_executor = await self._run_with({}, xtb_enabled=True)
        xtb_client.assert_not_called()
        xtb_executor.assert_not_called()
        paper.assert_called_once()

    async def test_real_account_needs_the_live_ack(self) -> None:
        """§7.41 / review L7: account_type 'real' alone must never reach real money."""
        creds = {"XTB_ACCOUNT_ID": "42", "XTB_ACCOUNT_PASSWORD": "v3r1fy"}
        original = _run_settings

        def real_settings(enabled: bool):
            s = original(enabled)
            s.xtb_execution.account_type = "real"
            return s

        with patch(f"{__name__}._run_settings", real_settings):
            xtb_client, paper, _ = await self._run_with(creds, xtb_enabled=True)
            xtb_client.assert_not_called()
            paper.assert_called_once()

            xtb_client, paper, _ = await self._run_with(
                {**creds, "LIVE_TRADING_ACK": "I_ACCEPT_REAL_MONEY_RISK"}, xtb_enabled=True
            )
            assert xtb_client.call_args.kwargs["account_type"] == "real"
            paper.assert_not_called()

    async def test_disabled_never_constructs_xtb_even_with_credentials(self) -> None:
        xtb_client, paper, _ = await self._run_with(
            {"XTB_ACCOUNT_ID": "42", "XTB_ACCOUNT_PASSWORD": "v3r1fy"},
            xtb_enabled=False,
        )
        xtb_client.assert_not_called()
        paper.assert_called_once()


class TestStartupPruning:
    """§7.12: with retention enabled, the runner prunes once at startup — even in --once mode."""

    async def test_run_once_prunes_before_the_cycle(self) -> None:
        settings = _run_settings(enabled=True)
        settings.storage.snapshot_retention_days = 30
        fake_agent = _FakeAgent()
        provider, executor, storage = _mock_env()
        storage.prune.return_value = {"market_snapshots": 3}

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("scripts.run_stocks_agent.Settings", return_value=settings),
            patch("scripts.run_stocks_agent.setup_logging"),
            patch("src.core.runner.Storage", return_value=storage),
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch("scripts.run_stocks_agent.create_xtb_provider", return_value=provider),
            patch("scripts.run_stocks_agent.PaperExecutor", return_value=executor),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_stocks_agent.StocksAgent", return_value=fake_agent),
        ):
            await run(run_once=True)

        storage.prune.assert_awaited_once_with(snapshot_days=30, history_days=0)
        assert fake_agent.cycles == 1
