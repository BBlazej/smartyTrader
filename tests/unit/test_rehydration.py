"""Tests for startup rehydration of paper + risk state from SQLite (§7.7)."""

from __future__ import annotations

import json

import pytest

from src.core.config import RiskSettings
from src.core.models import Position
from src.core.rehydration import (
    rehydrate_from_storage,
    rehydrate_paper_executor,
    rehydrate_risk_engine,
)
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor


@pytest.fixture()
def risk_settings() -> RiskSettings:
    return RiskSettings(
        max_position_pct=0.10,
        daily_loss_limit_pct=0.02,
        max_drawdown_pct=0.05,
        consecutive_losses_cooldown_minutes=60,
        max_open_positions=5,
        min_confidence=0.6,
    )


def _positions_json(*positions: Position) -> str:
    return json.dumps([p.model_dump(mode="json") for p in positions])


class TestPaperRehydration:
    async def test_restores_cash_and_positions(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            held = Position(
                symbol="BTC/USDT", quantity=2.0, avg_entry_price=50_000.0, current_price=51_000.0
            )
            await storage.save_portfolio_snapshot(
                cash=90_000.0,
                positions_json=_positions_json(held),
                total_value=192_000.0,
            )

            executor = PaperExecutor(initial_cash=100_000.0)
            assert await rehydrate_paper_executor(executor, storage) is True
            assert executor.cash == pytest.approx(90_000.0)
            positions = await executor.get_positions()
            assert len(positions) == 1
            assert positions[0].symbol == "BTC/USDT"
            assert positions[0].quantity == pytest.approx(2.0)
            # Marks survive too — the next cycle re-marks them again anyway.
            assert positions[0].current_price == pytest.approx(51_000.0)
        finally:
            await storage.close()

    async def test_no_snapshot_leaves_fresh_executor(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            executor = PaperExecutor(initial_cash=12_345.0)
            assert await rehydrate_paper_executor(executor, storage) is False
            assert executor.cash == pytest.approx(12_345.0)
            assert await executor.get_positions() == []
        finally:
            await storage.close()

    async def test_live_venue_executor_is_skipped(self, tmp_db_path: str) -> None:
        class VenueExecutor:  # no load_portfolio_state hook
            pass

        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            assert await rehydrate_paper_executor(VenueExecutor(), storage) is False
        finally:
            await storage.close()


class TestRiskEngineRehydration:
    async def test_daily_baseline_from_earliest_snapshot_today(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            await storage.save_portfolio_snapshot(
                cash=9_000.0, positions_json="[]", total_value=9_000.0
            )
            await storage.save_portfolio_snapshot(
                cash=8_700.0, positions_json="[]", total_value=8_700.0
            )

            engine = RiskEngine(risk_settings)
            await rehydrate_risk_engine(engine, storage)
            assert engine._daily_tracker.start_of_day_value == pytest.approx(9_000.0)

            # And the guard is honest after the restart: another drop below the
            # restored baseline trips the daily-loss rule again.
            signal = _active_signal()
            portfolio = _portfolio(8_600.0)
            result = engine.evaluate(signal, portfolio)
            assert result.verdict.value == "rejected"
            assert "daily loss" in (result.reason or "").lower()
        finally:
            await storage.close()

    async def test_loss_streak_and_cooldown_restored(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            for pnl in (-5.0, -4.0, -3.0):
                decision_id = await _save_decision(storage, "BTC/USDT")
                await storage.set_realized_pnl(decision_id, pnl)

            engine = RiskEngine(risk_settings)
            await rehydrate_risk_engine(engine, storage)

            assert engine._loss_tracker.consecutive_losses == 3
            assert engine._loss_tracker.in_cooldown  # loss timestamps are "now"

            signal = _active_signal()
            result = engine.evaluate(signal, _portfolio(10_000.0))
            assert result.verdict.value == "rejected"
            assert "cooldown" in (result.reason or "").lower()
        finally:
            await storage.close()

    async def test_cooldown_uses_configured_threshold_not_three(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        """§7.26: a non-default threshold must be honored across restarts."""
        risk_settings.consecutive_losses_threshold = 5
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            for pnl in (-5.0, -4.0, -3.0):
                decision_id = await _save_decision(storage, "BTC/USDT")
                await storage.set_realized_pnl(decision_id, pnl)

            engine = RiskEngine(risk_settings)
            await rehydrate_risk_engine(engine, storage)

            # Streak survives the restart, but 3 < threshold 5 → no cooldown.
            assert engine._loss_tracker.consecutive_losses == 3
            assert not engine._loss_tracker.in_cooldown
        finally:
            await storage.close()

    async def test_cooldown_restored_at_custom_threshold(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        """§7.26: reaching the configured threshold re-arms the cooldown."""
        risk_settings.consecutive_losses_threshold = 2
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            for pnl in (-5.0, -4.0):
                decision_id = await _save_decision(storage, "BTC/USDT")
                await storage.set_realized_pnl(decision_id, pnl)

            engine = RiskEngine(risk_settings)
            await rehydrate_risk_engine(engine, storage)

            assert engine._loss_tracker.consecutive_losses == 2
            assert engine._loss_tracker.in_cooldown
        finally:
            await storage.close()

    async def test_recent_win_breaks_the_streak(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            outcomes = [-5.0, -4.0, 2.0]  # newest last in save order → first when sorted desc
            for pnl in outcomes:
                decision_id = await _save_decision(storage, "BTC/USDT")
                await storage.set_realized_pnl(decision_id, pnl)

            engine = RiskEngine(risk_settings)
            await rehydrate_risk_engine(engine, storage)
            assert engine._loss_tracker.consecutive_losses == 0
            assert not engine._loss_tracker.in_cooldown
        finally:
            await storage.close()


class TestRehydrateFromStorage:
    async def test_covers_executor_and_engine(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            held = Position(
                symbol="ETH/USDT", quantity=3.0, avg_entry_price=10.0, current_price=9.0
            )
            await storage.save_portfolio_snapshot(
                cash=500.0, positions_json=_positions_json(held), total_value=527.0
            )
            decision_id = await _save_decision(storage, "ETH/USDT")
            await storage.set_realized_pnl(decision_id, -1.0)

            executor = PaperExecutor(initial_cash=1_000.0)
            engine = RiskEngine(risk_settings)
            await rehydrate_from_storage(engine, executor, storage)

            assert executor.cash == pytest.approx(500.0)
            assert (await executor.get_positions())[0].quantity == pytest.approx(3.0)
            assert engine._loss_tracker.consecutive_losses == 1
        finally:
            await storage.close()


async def _save_decision(storage: Storage, symbol: str) -> int:
    return await storage.save_llm_decision(
        symbol=symbol,
        action="sell",
        confidence=0.9,
        reasoning="closing",
        stop_loss=None,
        take_profit=None,
        risk_verdict="approved",
        risk_reason=None,
    )


def _active_signal():
    from src.core.models import Action, TradeSignal

    return TradeSignal(
        symbol="BTC/USDT", action=Action.BUY, confidence=0.9, reasoning="r", stop_loss=1.0
    )


def _portfolio(cash: float):
    from src.core.models import PortfolioState

    return PortfolioState(cash=cash, positions=[])
