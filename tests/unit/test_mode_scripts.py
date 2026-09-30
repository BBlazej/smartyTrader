"""§7.78 per-mode CLIs: --mode plumbing and book selection in the scripts."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scripts.prune_storage import run as prune_run
from scripts.rebaseline_drawdown import run as rebaseline_run
from src.core import db_layout
from src.core.config import Settings
from src.core.runner import ModeMismatch, RunnerAlreadyRunning
from src.core.storage import Storage
from tests.helpers import make_settings


def _settings_yaml(tmp_path: Path) -> str:
    make_settings(tmp_path, {"storage": {"data_dir": str(tmp_path / "books")}})
    return str(tmp_path / "settings.yaml")


class TestRunnerModeFlag:
    async def test_mode_is_passed_to_run_agent(self, tmp_path: Path) -> None:
        import scripts.run_crypto_agent as runner_script

        with (
            patch.object(runner_script, "load_dotenv"),
            patch.object(
                runner_script, "Settings", return_value=Settings(_settings_yaml(tmp_path))
            ),
            patch.object(runner_script, "setup_logging"),
            patch.object(runner_script, "run_agent", new=AsyncMock()) as run_agent,
        ):
            await runner_script.run(run_once=True, expected_mode="demo")
        assert run_agent.await_args.kwargs["expected_mode"] == "demo"

    @pytest.mark.parametrize(
        ("exc", "code"), [(RunnerAlreadyRunning("x"), 2), (ModeMismatch("x"), 3)]
    )
    def test_refusals_have_distinct_exit_codes(
        self, tmp_path: Path, exc: Exception, code: int
    ) -> None:
        # Synchronous: main() owns asyncio.run itself.
        import scripts.run_crypto_agent as runner_script

        with (
            patch.object(runner_script, "load_dotenv"),
            patch.object(
                runner_script, "Settings", return_value=Settings(_settings_yaml(tmp_path))
            ),
            patch.object(runner_script, "setup_logging"),
            patch.object(runner_script, "run_agent", new=AsyncMock(side_effect=exc)),
            patch.object(sys, "argv", ["run_crypto_agent", "--mode", "real"]),
            pytest.raises(SystemExit) as excinfo,
        ):
            runner_script.main()
        assert excinfo.value.code == code


class TestPruneScript:
    async def test_agent_without_mode_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            await prune_run(_settings_yaml(tmp_path), agent="crypto")

    async def test_every_book_is_pruned_with_its_own_mode(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "books"
        for mode in ("paper", "real"):
            book = Storage(str(db_layout.db_path(data_dir, mode, "crypto")))
            await book.initialize()
            await book.close()
        calls = []

        async def fake_prune(storage, settings, mode=None):
            calls.append(mode)
            return {}

        with patch("scripts.prune_storage.prune_storage", side_effect=fake_prune):
            await prune_run(_settings_yaml(tmp_path))
        # Each book is pruned with the mode of ITS OWN file — the real one is flagged
        # so retention never deletes its decision/order history (§7.78).
        assert calls == ["paper", "real"]

    async def test_single_book_selection(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "books"
        for mode in ("paper", "demo"):
            book = Storage(str(db_layout.db_path(data_dir, mode, "crypto")))
            await book.initialize()
            await book.close()
        calls = []

        async def fake_prune(storage, settings, mode=None):
            calls.append((mode, Path(storage.database_path).name))
            return {}

        with patch("scripts.prune_storage.prune_storage", side_effect=fake_prune):
            await prune_run(_settings_yaml(tmp_path), agent="crypto", mode="demo")
        assert calls == [("demo", "demo_crypto.db")]


class TestRebaselineBookSelection:
    async def test_reset_lands_in_the_selected_book(self, tmp_path: Path) -> None:
        config = _settings_yaml(tmp_path)
        data_dir = tmp_path / "books"
        book = Storage(
            str(db_layout.db_path(data_dir, "demo", "crypto")),
            agent="crypto",
            identity=("crypto", "demo"),
        )
        await book.initialize()
        await book.save_portfolio_snapshot(cash=4_000.0, positions_json="[]", total_value=4_600.0)
        await book.close()

        await rebaseline_run("crypto", None, True, config, mode="demo")

        reader = Storage(str(db_layout.db_path(data_dir, "demo", "crypto")))
        await reader.initialize()
        reset = await reader.get_drawdown_reset(agent="crypto")
        await reader.close()
        assert reset is not None and float(reset.baseline_value) == pytest.approx(4_600.0)


class TestBacktestBookSelection:
    async def test_missing_book_fails_before_anything_else(self, tmp_path: Path) -> None:
        import argparse

        from scripts.backtest import run as backtest_run

        args = argparse.Namespace(
            provider="ccxt",
            agent="crypto",
            mode="real",  # no real_crypto.db exists
            strategy=None,
            start=None,
            end=None,
            days=30,
            symbols=["BTC/EUR"],
            timeframe=None,
            report=None,
        )
        with (
            patch("scripts.backtest.Settings", return_value=Settings(_settings_yaml(tmp_path))),
            pytest.raises(SystemExit, match="no real book for the crypto agent"),
        ):
            await backtest_run(args)
