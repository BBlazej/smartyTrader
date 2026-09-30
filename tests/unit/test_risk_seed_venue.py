"""Risk-tracker seeds read only the bound venue's history (§7.76).

Found by the agent-driven OKX demo round trip: the crypto agent's paper history
(60 snapshots at the old 100,000 default) seeded the *demo* account's drawdown peak,
so every keyed BUY was rejected with a 95 % "drawdown". Every seed reads exactly the
bound venue's rows.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.rebaseline_drawdown import plan_rebaseline
from src.core.config import RiskSettings, Settings
from src.core.rehydration import rehydrate_loss_streak, rehydrate_risk_engine
from src.core.risk_engine import RiskEngine
from src.core.runner import run_agent
from src.core.storage import Storage

DEMO = "myokx-sandbox"


async def _snapshot(storage: Storage, value: float) -> None:
    await storage.save_portfolio_snapshot(cash=value, positions_json="[]", total_value=value)


@pytest.fixture()
async def history(tmp_path: Path) -> str:
    """Paper history at 100k, then a demo account at 4,600, in one file.

    Written into the demo book's file (§7.78) so the runner test opens exactly it —
    the mixed rows are the §7.76 scenario the venue scoping must still survive.
    """
    tmp_db_path = str(tmp_path / "demo_crypto.db")
    storage = Storage(tmp_db_path, agent="crypto", identity=("crypto", "demo"))
    await storage.initialize()
    storage.bind_venue("paper")
    for _ in range(3):
        await _snapshot(storage, 100_000.0)
    storage.bind_venue(DEMO)
    for _ in range(3):
        await _snapshot(storage, 4_600.0)
    await storage.close()
    return tmp_db_path


async def _bound(path: str, venue: str | None) -> Storage:
    storage = Storage(path, agent="crypto")
    await storage.initialize()
    storage.bind_venue(venue)
    return storage


class TestDrawdownPeak:
    async def test_keyed_venue_ignores_paper_history(self, history: str) -> None:
        storage = await _bound(history, DEMO)
        try:
            assert await storage.get_effective_peak_equity() == pytest.approx(4_600.0)
        finally:
            await storage.close()

    async def test_paper_reads_its_own_history(self, history: str) -> None:
        storage = await _bound(history, "paper")
        try:
            assert await storage.get_effective_peak_equity() == pytest.approx(100_000.0)
        finally:
            await storage.close()

    async def test_unbound_reads_every_venue(self, history: str) -> None:
        storage = await _bound(history, None)  # dashboard / CLI
        try:
            assert await storage.get_effective_peak_equity() == pytest.approx(100_000.0)
            assert await storage.get_effective_peak_equity(venue=DEMO) == pytest.approx(4_600.0)
        finally:
            await storage.close()

    async def test_demo_buy_is_no_longer_latched(self, history: str) -> None:
        # The exact §7.76 symptom: a 4,600 account against a 100k seed = -95 %.
        storage = await _bound(history, DEMO)
        try:
            engine = RiskEngine(RiskSettings())
            engine.seed_peak_equity(await storage.get_effective_peak_equity())
            assert engine.peak_equity == pytest.approx(4_600.0)
        finally:
            await storage.close()


class TestDrawdownResetVenue:
    async def test_a_paper_reset_does_not_move_the_demo_latch(self, history: str) -> None:
        storage = await _bound(history, None)
        await storage.record_drawdown_reset(baseline_value=90_000.0, agent="crypto", venue="paper")
        storage.bind_venue(DEMO)
        try:
            assert await storage.get_effective_peak_equity() == pytest.approx(4_600.0)
            storage.bind_venue("paper")
            assert await storage.get_effective_peak_equity() == pytest.approx(90_000.0)
        finally:
            await storage.close()

    async def test_a_demo_reset_applies_to_the_demo(self, history: str) -> None:
        storage = await _bound(history, None)
        await storage.record_drawdown_reset(baseline_value=4_000.0, agent="crypto", venue=DEMO)
        storage.bind_venue(DEMO)
        try:
            # History before the reset is cut off; nothing newer → the baseline.
            assert await storage.get_effective_peak_equity() == pytest.approx(4_000.0)
        finally:
            await storage.close()

    async def test_cli_plans_against_the_named_venue(self, history: str) -> None:
        storage = await _bound(history, None)
        try:
            old_seed, value = await plan_rebaseline(storage, "crypto", venue=DEMO)
            assert (old_seed, value) == (pytest.approx(4_600.0), pytest.approx(4_600.0))
        finally:
            await storage.close()


class TestDailyBaselineAndStreak:
    async def test_daily_baseline_is_the_venues_first_snapshot_today(self, history: str) -> None:
        storage = await _bound(history, DEMO)
        try:
            engine = RiskEngine(RiskSettings())
            await rehydrate_risk_engine(engine, storage)
            first = await storage.get_first_portfolio_snapshot_of_day()
            assert first is not None and first.venue == DEMO
            assert first.total_value == pytest.approx(4_600.0)
        finally:
            await storage.close()

    async def test_paper_losses_never_arm_the_demo_cooldown(self, tmp_db_path: str) -> None:
        storage = await _bound(tmp_db_path, "paper")
        for i in range(3):  # three paper losses: a full streak on paper
            await storage.save_order(
                f"paper-{i}", "BTC/EUR", "sell", 1.0, 90.0, "filled", realized_pnl=-10.0
            )
        try:
            paper = RiskEngine(RiskSettings(consecutive_losses_threshold=3))
            await rehydrate_loss_streak(paper, storage)
            assert paper._loss_tracker.consecutive_losses == 3

            storage.bind_venue(DEMO)
            demo = RiskEngine(RiskSettings(consecutive_losses_threshold=3))
            await rehydrate_loss_streak(demo, storage)
            assert demo._loss_tracker.consecutive_losses == 0
            assert await storage.get_recent_closing_fills() == []
        finally:
            await storage.close()


class TestRunnerSeedsAfterBinding:
    async def test_run_agent_seeds_the_demo_peak_from_demo_rows(
        self, tmp_path: Path, history: str
    ) -> None:
        config = tmp_path / "settings.yaml"
        config.write_text(
            f"""
llm: {{endpoint: "http://localhost:1234/v1/chat/completions", model: m}}
crypto_agent: {{enabled: true, interval_minutes: 5, pairs: ["BTC/EUR"], decision_history_limit: 10}}
stocks_agent: {{enabled: false, interval_minutes: 60, symbols: ["AAPL"], decision_history_limit: 10}}
risk: {{max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}}
storage: {{data_dir: "{Path(history).parent}"}}
monitoring: {{log_level: INFO}}
"""
        )
        engines: list[RiskEngine] = []

        def build_components() -> tuple[MagicMock, MagicMock]:
            provider = MagicMock()
            provider.close = AsyncMock()
            executor = MagicMock(spec=["venue", "close"])
            executor.venue = DEMO
            executor.close = AsyncMock()
            return provider, executor

        def build_agent(pipeline, storage, risk_engine, llm_client) -> MagicMock:
            engines.append(risk_engine)
            agent = MagicMock(spec=["run_cycle", "shutdown"])
            agent.run_cycle = AsyncMock(return_value=[])
            agent.shutdown = AsyncMock()
            return agent

        await run_agent(
            Settings(str(config)),
            component="crypto",
            agent_enabled=True,
            interval_minutes=5,
            decision_history_limit=10,
            job_id="crypto_cycle",
            build_components=build_components,
            build_agent=build_agent,
            run_once=True,
        )
        assert engines[0].peak_equity == pytest.approx(4_600.0)
