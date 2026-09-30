"""One database per agent × trading mode (§7.78): layout, identity guard, runner wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core import db_layout
from src.core.config import StorageSettings
from src.core.retention import prune_storage
from src.core.storage import DatabaseIdentityError, Storage
from tests.helpers import make_settings


class TestVenueMode:
    @pytest.mark.parametrize(
        ("venue", "mode"),
        [
            (None, "paper"),
            ("paper", "paper"),
            ("myokx-sandbox", "demo"),
            ("saxo-sim", "demo"),
            ("xtb-demo", "demo"),
            ("myokx-live", "real"),
            ("myokx-LIVE", "real"),
            ("saxo-live", "real"),
            ("xtb-real", "real"),
        ],
    )
    def test_every_venue_label_maps_to_its_mode(self, venue: str | None, mode: str) -> None:
        assert db_layout.venue_mode(venue) == mode

    def test_unknown_labels_are_refused_not_guessed(self) -> None:
        with pytest.raises(db_layout.UnknownVenueError):
            db_layout.venue_mode("somewhere")

    def test_paths_and_keys(self, tmp_path: Path) -> None:
        assert db_layout.db_path(tmp_path, "demo", "crypto") == tmp_path / "demo_crypto.db"
        assert db_layout.lock_path(tmp_path, "real", "stocks").name == "real_stocks.runner.lock"
        assert db_layout.parse_book_key("demo_crypto") == ("demo", "crypto")
        assert db_layout.parse_book_key("trading_agent") is None
        with pytest.raises(ValueError):
            db_layout.book_key("live", "crypto")

    def test_discover_lists_only_book_files(self, tmp_path: Path) -> None:
        for name in ("real_crypto.db", "paper_crypto.db", "paper_stocks.db", "trading_agent.db"):
            (tmp_path / name).touch()
        (tmp_path / "demo_crypto.db-wal").touch()
        found = [(m, a) for m, a, _ in db_layout.discover(tmp_path)]
        assert found == [("paper", "crypto"), ("real", "crypto"), ("paper", "stocks")]
        assert [(m, a) for m, a, _ in db_layout.discover(tmp_path, ["stocks"])] == [
            ("paper", "stocks")
        ]


class TestStorageSettings:
    def test_data_dir_and_in_memory(self) -> None:
        assert StorageSettings(data_dir="books").data_dir == "books"
        assert StorageSettings().data_dir == "data" and StorageSettings().in_memory is False
        assert StorageSettings(in_memory=True).in_memory is True


class TestIdentityGuard:
    async def test_a_new_file_is_stamped_and_reopened(self, tmp_path: Path) -> None:
        path = str(tmp_path / "demo_crypto.db")
        first = Storage(path, agent="crypto", identity=("crypto", "demo"))
        await first.initialize()
        await first.save_portfolio_snapshot(cash=1.0, positions_json="[]", total_value=1.0)
        assert await first.get_identity() == ("crypto", "demo")
        await first.close()

        again = Storage(path, agent="crypto", identity=("crypto", "demo"))
        await again.initialize()  # same book: fine
        await again.close()

    @pytest.mark.parametrize("identity", [("crypto", "real"), ("stocks", "demo")])
    async def test_a_foreign_book_is_refused(
        self, tmp_path: Path, identity: tuple[str, str]
    ) -> None:
        path = str(tmp_path / "demo_crypto.db")
        owner = Storage(path, identity=("crypto", "demo"))
        await owner.initialize()
        await owner.close()
        intruder = Storage(path, identity=identity)
        with pytest.raises(DatabaseIdentityError, match="holds the demo crypto book"):
            await intruder.initialize()
        await intruder.close()

    async def test_readers_open_any_file_without_checking(self, tmp_path: Path) -> None:
        path = str(tmp_path / "demo_crypto.db")
        owner = Storage(path, identity=("crypto", "demo"))
        await owner.initialize()
        await owner.close()
        reader = Storage(path)  # dashboard / CLI
        await reader.initialize()
        assert await reader.get_identity() == ("crypto", "demo")
        await reader.close()


class TestRunnerModes:
    @staticmethod
    def _settings(tmp_path: Path):
        return make_settings(tmp_path)

    @staticmethod
    def _components(venue: str | None, closed: list[str]):
        def build():
            provider = MagicMock(spec=["close"])
            provider.close = AsyncMock(side_effect=lambda: closed.append("provider"))
            executor = MagicMock(spec=["venue", "close"])
            executor.venue = venue
            executor.close = AsyncMock(side_effect=lambda: closed.append("executor"))
            return provider, executor

        return build

    async def _run(self, tmp_path: Path, venue: str | None, **kwargs) -> list[str]:
        from src.core.runner import run_agent

        closed: list[str] = []
        agent = MagicMock(spec=["run_cycle", "shutdown"])
        agent.run_cycle = AsyncMock(return_value=[])
        agent.shutdown = AsyncMock()
        await run_agent(
            self._settings(tmp_path),
            component="crypto",
            agent_enabled=True,
            interval_minutes=5,
            decision_history_limit=10,
            job_id="crypto_cycle",
            build_components=self._components(venue, closed),
            build_agent=lambda *_: agent,
            run_once=True,
            **kwargs,
        )
        return closed

    async def test_the_executor_venue_picks_the_file(self, tmp_path: Path) -> None:
        await self._run(tmp_path, "myokx-sandbox")
        assert (tmp_path / "demo_crypto.db").exists()
        assert not (tmp_path / "paper_crypto.db").exists()
        reader = Storage(str(tmp_path / "demo_crypto.db"))
        await reader.initialize()
        assert await reader.get_identity() == ("crypto", "demo")
        await reader.close()

    async def test_expected_mode_mismatch_refuses_before_any_db(self, tmp_path: Path) -> None:
        from src.core.runner import ModeMismatch

        with pytest.raises(ModeMismatch, match="asked for demo"):
            await self._run(tmp_path, "paper", expected_mode="demo")
        assert list(tmp_path.glob("*.db")) == []

    async def test_unknown_venue_refuses_and_closes_components(self, tmp_path: Path) -> None:
        from src.core.runner import run_agent

        closed: list[str] = []
        with pytest.raises(db_layout.UnknownVenueError):
            await run_agent(
                self._settings(tmp_path),
                component="crypto",
                agent_enabled=True,
                interval_minutes=5,
                decision_history_limit=10,
                job_id="crypto_cycle",
                build_components=self._components("mystery", closed),
                build_agent=lambda *_: MagicMock(),
                run_once=True,
            )
        assert closed == ["provider", "executor"]
        assert list(tmp_path.glob("*.db")) == []


class TestRealBookRetention:
    async def test_real_history_is_never_pruned(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.prune = AsyncMock(return_value={})
        settings = StorageSettings(
            data_dir=str(tmp_path), snapshot_retention_days=30, history_retention_days=90
        )
        await prune_storage(storage, settings, mode="real")
        assert storage.prune.await_args.kwargs == {"snapshot_days": 30, "history_days": 0}
        await prune_storage(storage, settings, mode="paper")
        assert storage.prune.await_args.kwargs == {"snapshot_days": 30, "history_days": 90}
