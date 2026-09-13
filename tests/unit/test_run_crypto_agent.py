"""Tests for the entry-point helper functions."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_crypto_agent import _build_data_and_execution, _load_dotenv


class TestLoadDotenv:
    def test_loads_simple_pairs(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write("FOO_TEST_KEY=bar\nBAZ_TEST_KEY=qux\n")

        os.environ.pop("FOO_TEST_KEY", None)
        os.environ.pop("BAZ_TEST_KEY", None)
        _load_dotenv(env_file)

        assert os.environ["FOO_TEST_KEY"] == "bar"
        assert os.environ["BAZ_TEST_KEY"] == "qux"
        os.environ.pop("FOO_TEST_KEY", None)
        os.environ.pop("BAZ_TEST_KEY", None)

    def test_ignores_comments_and_blanks(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write("# full-line comment\n\nREAL_KEY=value\n\n")

        os.environ.pop("REAL_KEY", None)
        _load_dotenv(env_file)
        assert os.environ["REAL_KEY"] == "value"
        os.environ.pop("REAL_KEY", None)

    def test_strips_surrounding_quotes(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write('QUOTED_TEST_KEY="hello world"\n')

        os.environ.pop("QUOTED_TEST_KEY", None)
        _load_dotenv(env_file)
        assert os.environ["QUOTED_TEST_KEY"] == "hello world"
        os.environ.pop("QUOTED_TEST_KEY", None)

    def test_existing_env_var_wins(self, tmp_path: object) -> None:
        env_file = os.path.join(str(tmp_path), "test.env")
        with open(env_file, "w") as f:
            f.write("PRIORITY_TEST_KEY=from_file\n")

        os.environ["PRIORITY_TEST_KEY"] = "from_real_env"
        _load_dotenv(env_file)
        assert os.environ["PRIORITY_TEST_KEY"] == "from_real_env"
        os.environ.pop("PRIORITY_TEST_KEY", None)

    def test_missing_file_is_noop(self, tmp_path: object) -> None:
        # Should not raise.
        _load_dotenv(os.path.join(str(tmp_path), "does_not_exist.env"))


def _settings(
    exchange: str = "kraken",
    testnet: bool = True,
    fee_pct: float = 0.0026,
    slippage_pct: float = 0.001,
) -> SimpleNamespace:
    """A minimal settings stub with just the attributes the helper reads."""
    return SimpleNamespace(
        crypto_agent=SimpleNamespace(exchange=exchange, testnet=testnet),
        execution=SimpleNamespace(paper_fee_pct=fee_pct, paper_slippage_pct=slippage_pct),
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
            patch("scripts.run_crypto_agent.create_kraken_executor") as mock_executor,
        ):
            _, executor, mode = _build_data_and_execution(settings)

        assert mode == "paper"
        assert isinstance(executor, PaperExecutor)
        # The data feed must be built against live public data (no sandbox mode).
        mock_provider.assert_called_once_with(exchange_id="kraken", testnet=False)
        # No API key → the Kraken executor is never constructed.
        mock_executor.assert_not_called()

    def test_api_key_selects_kraken_testnet(self) -> None:
        from src.execution.kraken_executor import KrakenExecutor

        settings = _settings(testnet=True)
        fake_order_client = object()
        data_provider = SimpleNamespace(client=object())
        order_provider = SimpleNamespace(client=fake_order_client)

        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            patch("scripts.run_crypto_agent.create_kraken_executor") as mock_executor,
            patch.dict(
                os.environ, {"KRAKEN_API_KEY": "key", "KRAKEN_API_SECRET": "secret"}, clear=False
            ),
        ):
            # First call builds the public data feed; the second builds the keyed
            # order-placement client. Return distinct stand-ins to assert roles.
            mock_provider.side_effect = [data_provider, order_provider]
            mock_executor.return_value = KrakenExecutor(client=None)

            provider, executor, mode = _build_data_and_execution(settings)

        assert mode == "kraken-testnet"
        assert isinstance(executor, KrakenExecutor)
        assert provider is data_provider
        # The Kraken executor is built on the dedicated keyed client (not the data one).
        mock_executor.assert_called_once_with(fake_order_client)
        calls = mock_provider.call_args_list
        assert len(calls) == 2
        # Public data feed: no sandbox, no keys.
        assert calls[0].kwargs == {"exchange_id": "kraken", "testnet": False}
        # Order feed: sandboxed per config + keyed.
        assert calls[1].kwargs == {
            "exchange_id": "kraken",
            "testnet": True,
            "api_key": "key",
            "api_secret": "secret",
        }

    def test_paper_executor_gets_configured_costs(self) -> None:
        from src.execution.paper_executor import PaperExecutor

        settings = _settings(fee_pct=0.005, slippage_pct=0.002)
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider"),
            patch("scripts.run_crypto_agent.create_kraken_executor"),
        ):
            _, executor, _ = _build_data_and_execution(settings)

        assert isinstance(executor, PaperExecutor)
        assert executor.fee_pct == 0.005
        assert executor.slippage_pct == 0.002

    def test_exchange_defaults_to_kraken_when_unset(self) -> None:
        settings = _settings(exchange=None)  # type: ignore[arg-type]
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            patch("scripts.run_crypto_agent.create_kraken_executor"),
        ):
            _build_data_and_execution(settings)

        assert mock_provider.call_args_list[0].kwargs["exchange_id"] == "kraken"

    def test_explicit_exchange_is_honored(self) -> None:
        settings = _settings(exchange="binance")
        with (
            patch("scripts.run_crypto_agent.create_ccxt_provider") as mock_provider,
            patch("scripts.run_crypto_agent.create_kraken_executor"),
        ):
            _build_data_and_execution(settings)

        # Both the data feed and (in testnet mode) the order feed use the configured exchange.
        assert mock_provider.call_args_list[0].kwargs["exchange_id"] == "binance"
