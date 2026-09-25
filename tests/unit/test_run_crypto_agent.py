"""Tests for the entry-point helper functions."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.run_crypto_agent import _build_data_and_execution, run
from src.core.runner import load_dotenv


def _storage_mock() -> AsyncMock:
    """Async Storage stand-in; ``bind_venue`` is the one sync method (§7.61)."""
    storage = AsyncMock()
    storage.bind_venue = MagicMock()
    return storage


class TestLoadDotenv:
    def test_loads_simple_pairs(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write("FOO_TEST_KEY=bar\nBAZ_TEST_KEY=qux\n")

        os.environ.pop("FOO_TEST_KEY", None)
        os.environ.pop("BAZ_TEST_KEY", None)
        load_dotenv(env_file)

        assert os.environ["FOO_TEST_KEY"] == "bar"
        assert os.environ["BAZ_TEST_KEY"] == "qux"
        os.environ.pop("FOO_TEST_KEY", None)
        os.environ.pop("BAZ_TEST_KEY", None)

    def test_ignores_comments_and_blanks(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write("# full-line comment\n\nREAL_KEY=value\n\n")

        os.environ.pop("REAL_KEY", None)
        load_dotenv(env_file)
        assert os.environ["REAL_KEY"] == "value"
        os.environ.pop("REAL_KEY", None)

    def test_strips_surrounding_quotes(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write('QUOTED_TEST_KEY="hello world"\n')

        os.environ.pop("QUOTED_TEST_KEY", None)
        load_dotenv(env_file)
        assert os.environ["QUOTED_TEST_KEY"] == "hello world"
        os.environ.pop("QUOTED_TEST_KEY", None)

    def test_existing_env_var_wins(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write("PRIORITY_TEST_KEY=from_file\n")

        os.environ["PRIORITY_TEST_KEY"] = "from_real_env"
        load_dotenv(env_file)
        assert os.environ["PRIORITY_TEST_KEY"] == "from_real_env"
        os.environ.pop("PRIORITY_TEST_KEY", None)

    def test_missing_file_is_noop(self, tmp_path: object) -> None:
        # Should not raise.
        load_dotenv(os.path.join(str(tmp_path), "does_not_exist.env"))


def _settings(
    exchange: str = "myokx",
    testnet: bool = True,
    fee_pct: float = 0.0026,
    slippage_pct: float = 0.001,
    initial_cash: float = 100_000.0,
) -> SimpleNamespace:
    """A minimal settings stub with just the attributes the helper reads."""
    return SimpleNamespace(
        crypto_agent=SimpleNamespace(exchange=exchange, testnet=testnet, quote_currency="EUR"),
        execution=SimpleNamespace(
            paper_fee_pct=fee_pct,
            paper_slippage_pct=slippage_pct,
            initial_cash=initial_cash,
        ),
    )


class TestBuildDataAndExecution:
    """The data feed is live public data in both modes; only execution changes.

    These mock the CCXT/executor factories so no network or the optional ccxt
    dependency is needed — they assert *which* client is built for *which* role.
    """

    def test_no_api_key_selects_paper(self) -> None:
        from src.execution.paper_executor import PaperExecutor

        settings = _settings()
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            patch("scripts.run_crypto_agent.create_ccxt_executor") as mock_executor,
        ):
            _, executor, mode = _build_data_and_execution(settings)

        assert mode == "paper"
        assert isinstance(executor, PaperExecutor)
        # The data feed must be built against live public data (no sandbox mode).
        mock_provider.assert_called_once_with(exchange_id="myokx", testnet=False)
        # No API key → the keyed executor is never constructed.
        mock_executor.assert_not_called()

    # ── §7.41: keyed execution never reaches real money by accident ──────

    def _keyed(self, settings, *, has_sandbox: bool, env: dict[str, str]):
        """Run the selector with a key set; returns (executor, mode, provider-mock)."""
        order_provider = SimpleNamespace(client=object())
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            patch("scripts.run_crypto_agent.create_ccxt_executor") as mock_executor,
            patch("scripts.run_crypto_agent.exchange_has_sandbox", return_value=has_sandbox),
            patch.dict(
                os.environ,
                {"EXCHANGE_API_KEY": "key", "EXCHANGE_API_SECRET": "secret", **env},
                clear=True,
            ),
        ):
            mock_provider.side_effect = [SimpleNamespace(client=object()), order_provider]
            mock_executor.return_value = "KEYED"
            _, executor, mode = _build_data_and_execution(settings)
        return executor, mode, mock_provider

    def test_key_on_exchange_without_sandbox_stays_on_paper(self) -> None:
        from src.execution.paper_executor import PaperExecutor

        # testnet: true on an exchange ccxt has no sandbox for → never keyed.
        executor, mode, provider = self._keyed(
            _settings(exchange="kraken", testnet=True), has_sandbox=False, env={}
        )
        assert mode == "paper" and isinstance(executor, PaperExecutor)
        assert provider.call_count == 1  # the keyed client is never even built

    def test_key_with_real_sandbox_uses_it(self) -> None:
        # The shipped default: OKX Europe demo trading (§7.64), incl. the passphrase.
        executor, mode, provider = self._keyed(
            _settings(testnet=True),
            has_sandbox=True,
            env={"EXCHANGE_API_PASSPHRASE": "phrase"},
        )
        assert mode == "myokx-sandbox" and executor == "KEYED"
        assert provider.call_args_list[1].kwargs == {
            "exchange_id": "myokx",
            "testnet": True,
            "api_key": "key",
            "api_secret": "secret",
            "api_passphrase": "phrase",
        }

    def test_keyed_executor_gets_quote_currency_and_venue_tag(self) -> None:
        order_provider = SimpleNamespace(client=object())
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            patch("scripts.run_crypto_agent.create_ccxt_executor") as mock_executor,
            patch("scripts.run_crypto_agent.exchange_has_sandbox", return_value=True),
            patch.dict(os.environ, {"EXCHANGE_API_KEY": "key"}, clear=True),
        ):
            mock_provider.side_effect = [SimpleNamespace(client=object()), order_provider]
            _build_data_and_execution(_settings(testnet=True))
        mock_executor.assert_called_once_with(
            order_provider.client, quote_currency="EUR", venue="myokx-sandbox"
        )

    def test_old_kraken_env_names_are_not_read(self) -> None:
        from src.execution.paper_executor import PaperExecutor

        # §7.64: Kraken was dropped without fallbacks — its variables configure nothing.
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider"),
            patch("scripts.run_crypto_agent.create_ccxt_executor") as mock_executor,
            patch.dict(os.environ, {"KRAKEN_API_KEY": "key"}, clear=True),
        ):
            _, executor, mode = _build_data_and_execution(_settings(testnet=True))
        assert mode == "paper" and isinstance(executor, PaperExecutor)
        mock_executor.assert_not_called()

    def test_testnet_false_alone_never_trades_live(self) -> None:
        from src.execution.paper_executor import PaperExecutor

        executor, mode, _ = self._keyed(_settings(testnet=False), has_sandbox=False, env={})
        assert mode == "paper" and isinstance(executor, PaperExecutor)

    def test_live_flag_without_env_ack_stays_on_paper(self) -> None:
        settings = _settings(testnet=False)
        settings.crypto_agent.live_trading = True
        _, mode, _ = self._keyed(settings, has_sandbox=False, env={"LIVE_TRADING_ACK": "yes"})
        assert mode == "paper"

    def test_both_opt_ins_select_the_live_venue(self) -> None:
        settings = _settings(testnet=False)
        settings.crypto_agent.live_trading = True
        executor, mode, provider = self._keyed(
            settings,
            has_sandbox=False,
            env={"LIVE_TRADING_ACK": "I_ACCEPT_REAL_MONEY_RISK"},
        )
        assert mode == "myokx-LIVE" and executor == "KEYED"
        assert provider.call_args_list[1].kwargs["testnet"] is False

    def test_paper_executor_gets_configured_costs(self) -> None:
        from src.execution.paper_executor import PaperExecutor

        settings = _settings(fee_pct=0.005, slippage_pct=0.002, initial_cash=25_000.0)
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider"),
            patch("scripts.run_crypto_agent.create_ccxt_executor"),
        ):
            _, executor, _ = _build_data_and_execution(settings)

        assert isinstance(executor, PaperExecutor)
        assert executor.fee_pct == 0.005
        assert executor.slippage_pct == 0.002
        # initial_cash is config-driven (§7.7), not hardcoded in the executor.
        assert executor.cash == 25_000.0

    def test_unset_exchange_fails_fast(self) -> None:
        settings = _settings(exchange=None)  # type: ignore[arg-type]
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            pytest.raises(SystemExit, match="crypto_agent.exchange"),
        ):
            _build_data_and_execution(settings)
        mock_provider.assert_not_called()

    def test_explicit_exchange_is_honored(self) -> None:
        settings = _settings(exchange="binance")
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            patch("scripts.run_crypto_agent.create_ccxt_executor"),
        ):
            _build_data_and_execution(settings)

        # Both the data feed and (in testnet mode) the order feed use the configured exchange.
        assert mock_provider.call_args_list[0].kwargs["exchange_id"] == "binance"


# ── enabled / --once semantics [R-H3] ─────────────────────────────


def _run_settings(enabled: bool) -> SimpleNamespace:
    """A settings stub covering everything ``run()`` reads before the agent."""
    return SimpleNamespace(
        llm=SimpleNamespace(),
        crypto_agent=SimpleNamespace(
            enabled=enabled,
            exchange="myokx",
            testnet=True,
            quote_currency="EUR",
            interval_minutes=5,
            pairs=["BTC/USDT"],
            decision_history_limit=10,
            timeframe="1h",
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


class TestEnabledSemantics:
    """``enabled: false`` must run *nothing*; single-cycle is an explicit ``--once``."""

    async def test_disabled_exits_before_constructing_anything(self) -> None:
        settings = _run_settings(enabled=False)
        with (
            patch("scripts.run_crypto_agent.Settings", return_value=settings),
            patch("scripts.run_crypto_agent.setup_logging"),
            patch("src.core.runner.Storage") as mock_storage,
            patch("src.core.runner.LLMClient") as mock_llm,
            patch("scripts.run_crypto_agent._build_data_and_execution") as mock_build,
            patch("src.core.runner.DecisionPipeline") as mock_pipeline,
            patch("scripts.run_crypto_agent.CryptoAgent") as mock_agent_cls,
        ):
            await run()

        # A disabled agent must not touch the DB, the LLM, the provider/executor,
        # the pipeline or the agent itself — no cycles, orders or writes.
        mock_storage.assert_not_called()
        mock_llm.assert_not_called()
        mock_build.assert_not_called()
        mock_pipeline.assert_not_called()
        mock_agent_cls.assert_not_called()

    async def test_run_once_runs_exactly_one_cycle_then_shuts_down(self) -> None:
        settings = _run_settings(enabled=True)
        fake_agent = _FakeAgent()
        storage = _storage_mock()
        provider = MagicMock()
        provider.close = AsyncMock()
        executor = MagicMock()
        executor.close = AsyncMock()

        with (
            patch("scripts.run_crypto_agent.Settings", return_value=settings),
            patch("scripts.run_crypto_agent.setup_logging"),
            patch("src.core.runner.Storage", return_value=storage) as storage_cls,
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch(
                "scripts.run_crypto_agent._build_data_and_execution",
                return_value=(provider, executor, "paper"),
            ),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_crypto_agent.CryptoAgent", return_value=fake_agent),
            patch("src.core.scheduler.AsyncSchedulerManager") as mock_manager_cls,
        ):
            await run(run_once=True)

        assert fake_agent.cycles == 1
        assert fake_agent.shutdowns == 1
        provider.close.assert_awaited_once()
        executor.close.assert_awaited_once()
        storage.close.assert_awaited_once()
        # Agent-bound storage (§7.39): every row this runner writes/reads is its own.
        assert storage_cls.call_args.kwargs["agent"] == "crypto"
        # Single-cycle mode never starts the scheduler.
        mock_manager_cls.assert_not_called()

    async def test_run_once_failure_still_releases_resources(self) -> None:
        settings = _run_settings(enabled=True)
        fake_agent = _FakeAgent(fail_cycle=True)
        storage = _storage_mock()
        provider = MagicMock()
        provider.close = AsyncMock()
        executor = MagicMock()
        executor.close = AsyncMock()

        with (
            patch("scripts.run_crypto_agent.Settings", return_value=settings),
            patch("scripts.run_crypto_agent.setup_logging"),
            patch("src.core.runner.Storage", return_value=storage),
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch(
                "scripts.run_crypto_agent._build_data_and_execution",
                return_value=(provider, executor, "paper"),
            ),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_crypto_agent.CryptoAgent", return_value=fake_agent),
            pytest.raises(RuntimeError, match="cycle boom"),
        ):
            await run(run_once=True)

        # A failed --once cycle propagates (non-zero exit) but still cleans up.
        provider.close.assert_awaited_once()
        executor.close.assert_awaited_once()
        storage.close.assert_awaited_once()


class TestScheduledModeLifecycle:
    """The shared scheduled loop (§7.13): start, first cycle, jobs, cleanup on cancel."""

    async def test_scheduled_loop_runs_starts_jobs_and_cleans_up(self) -> None:
        import asyncio

        settings = _run_settings(enabled=True)
        settings.storage.snapshot_retention_days = 30
        settings.storage.prune_interval_minutes = 1440
        fake_agent = _FakeAgent()
        storage = _storage_mock()
        storage.prune.return_value = {"market_snapshots": 0}
        provider = MagicMock()
        provider.close = AsyncMock()
        executor = MagicMock()
        executor.close = AsyncMock()
        manager = MagicMock()

        with (
            patch("scripts.run_crypto_agent.Settings", return_value=settings),
            patch("scripts.run_crypto_agent.setup_logging"),
            patch("src.core.runner.Storage", return_value=storage),
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch(
                "scripts.run_crypto_agent._build_data_and_execution",
                return_value=(provider, executor, "paper"),
            ),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_crypto_agent.CryptoAgent", return_value=fake_agent),
            patch("src.core.scheduler.AsyncSchedulerManager", return_value=manager),
            patch("src.core.scheduler.create_async_scheduler", return_value=MagicMock()),
        ):
            task = asyncio.create_task(run())
            await asyncio.sleep(0.05)  # let the loop reach its Event wait

            assert fake_agent.starts == 1
            assert fake_agent.cycles >= 1  # first cycle runs immediately (§7.2)
            manager.start.assert_called_once()
            job_ids = {c.kwargs.get("job_id") for c in manager.schedule_cycle.call_args_list}
            assert {"crypto_cycle", "storage_prune"} <= job_ids

            task.cancel()
            await task  # CancelledError is caught; cleanup runs in finally

        assert fake_agent.shutdowns == 1
        manager.shutdown.assert_called_once()
        provider.close.assert_awaited_once()
        executor.close.assert_awaited_once()
        storage.close.assert_awaited_once()


class TestStartupPruning:
    """§7.12: with retention enabled, the runner prunes once at startup — even in --once mode."""

    async def test_run_once_prunes_before_the_cycle(self) -> None:
        settings = _run_settings(enabled=True)
        settings.storage.snapshot_retention_days = 30
        fake_agent = _FakeAgent()
        storage = _storage_mock()
        storage.prune.return_value = {"market_snapshots": 3}
        provider = MagicMock()
        provider.close = AsyncMock()
        executor = MagicMock()
        executor.close = AsyncMock()

        with (
            patch("scripts.run_crypto_agent.Settings", return_value=settings),
            patch("scripts.run_crypto_agent.setup_logging"),
            patch("src.core.runner.Storage", return_value=storage),
            patch("src.core.runner.LLMClient", return_value=MagicMock()),
            patch("src.core.runner.RiskEngine"),
            patch(
                "scripts.run_crypto_agent._build_data_and_execution",
                return_value=(provider, executor, "paper"),
            ),
            patch("src.core.runner.DecisionPipeline"),
            patch("scripts.run_crypto_agent.CryptoAgent", return_value=fake_agent),
        ):
            await run(run_once=True)

        storage.prune.assert_awaited_once_with(snapshot_days=30, history_days=0)
        assert fake_agent.cycles == 1
