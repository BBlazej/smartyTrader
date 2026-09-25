"""Tests for startup rehydration of paper + risk state from SQLite (§7.7)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.core.config import RiskSettings
from src.core.models import OrderSide, Position
from src.core.rehydration import (
    executor_venue,
    rehydrate_from_storage,
    rehydrate_paper_executor,
    rehydrate_risk_engine,
    rehydrate_venue_executor,
)
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.ccxt_executor import CcxtExecutor
from src.execution.paper_executor import PaperExecutor
from src.execution.xtb_executor import XTBExecutor


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


class TestFillLedgerRehydration:
    """§7.25: FIFO lots + entry decision ids survive a restart."""

    async def test_closed_entries_survive_restart(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            book = PaperExecutor(initial_cash=100_000.0, slippage_pct=0.0)

            # Pre-restart history: two buys (decisions 1 and 2), one partial sell.
            b1 = await book.place_order("BTC/USDT", OrderSide.BUY, 0.5, 50_000.0, decision_id=1)
            await storage.save_order(
                order_id=b1.order_id,
                symbol="BTC/USDT",
                side="buy",
                quantity=0.5,
                price=b1.price,
                status="filled",
                decision_id=1,
                filled_at=b1.filled_at,
            )
            b2 = await book.place_order("BTC/USDT", OrderSide.BUY, 0.5, 60_000.0, decision_id=2)
            await storage.save_order(
                order_id=b2.order_id,
                symbol="BTC/USDT",
                side="buy",
                quantity=0.5,
                price=b2.price,
                status="filled",
                decision_id=2,
                filled_at=b2.filled_at,
            )
            s1 = await book.place_order("BTC/USDT", OrderSide.SELL, 0.7, 70_000.0)
            await storage.save_order(
                order_id=s1.order_id,
                symbol="BTC/USDT",
                side="sell",
                quantity=0.7,
                price=s1.price,
                status="filled",
                decision_id=None,
                filled_at=s1.filled_at,
            )
            assert s1.realized_pnl == pytest.approx(12_000.0)

            positions = await book.get_positions()
            await storage.save_portfolio_snapshot(
                cash=book.cash,
                positions_json=_positions_json(*positions),
                total_value=book.cash + sum(p.quantity * p.current_price for p in positions),
            )

            # Restart: fresh executor rehydrates cash, position AND lot ledger.
            revived = PaperExecutor(initial_cash=100_000.0, slippage_pct=0.0)
            assert await rehydrate_paper_executor(revived, storage) is True

            s2 = await revived.place_order("BTC/USDT", OrderSide.SELL, 0.3, 80_000.0)
            assert s2.status == "filled"
            # Remaining 0.3 units are FIFO-wise the tail of lot 2 (@60k, decision 2).
            assert s2.realized_pnl == pytest.approx((80_000.0 - 60_000.0) * 0.3)
            entries = {e.entry_decision_id: e.pnl for e in s2.closed_entries}
            assert entries == {2: pytest.approx(6_000.0)}
        finally:
            await storage.close()

    async def test_pruned_history_falls_back_to_synthetic_lots(self, tmp_db_path: str) -> None:
        """No stored fills → one synthetic lot per position at avg entry (basis kept)."""
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            held = Position(
                symbol="ETH/USDT", quantity=2.0, avg_entry_price=20.0, current_price=22.0
            )
            await storage.save_portfolio_snapshot(
                cash=100.0, positions_json=_positions_json(held), total_value=144.0
            )

            revived = PaperExecutor(initial_cash=1_000.0, slippage_pct=0.0)
            assert await rehydrate_paper_executor(revived, storage) is True
            sold = await revived.place_order("ETH/USDT", OrderSide.SELL, 2.0, 30.0)
            assert sold.status == "filled"
            assert sold.realized_pnl == pytest.approx((30.0 - 20.0) * 2.0)
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
                await _save_closing_fill(storage, "BTC/USDT", pnl)

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
                await _save_closing_fill(storage, "BTC/USDT", pnl)

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
                await _save_closing_fill(storage, "BTC/USDT", pnl)

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
                await _save_closing_fill(storage, "BTC/USDT", pnl)

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
            await _save_closing_fill(storage, "ETH/USDT", -1.0)

            executor = PaperExecutor(initial_cash=1_000.0)
            engine = RiskEngine(risk_settings)
            await rehydrate_from_storage(engine, executor, storage)

            assert executor.cash == pytest.approx(500.0)
            assert (await executor.get_positions())[0].quantity == pytest.approx(3.0)
            assert engine._loss_tracker.consecutive_losses == 1
        finally:
            await storage.close()


async def _save_closing_fill(storage: Storage, symbol: str, pnl: float) -> None:
    """A closing sell fill that realized ``pnl`` — what the live streak counts (§7.46)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    await storage.save_order(
        order_id=f"close-{uuid4().hex[:8]}",
        symbol=symbol,
        side="sell",
        quantity=1.0,
        price=100.0,
        status="filled",
        filled_at=datetime.now(UTC),
        realized_pnl=pnl,
    )


async def _save_decision(storage: Storage, symbol: str, action: str = "sell") -> int:
    return await storage.save_llm_decision(
        symbol=symbol,
        action=action,
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


class TestStreakMatchesLiveCounting:
    """§7.46: restart rebuilds the streak from closing fills — one per closing fill,
    exactly what the live tracker counts — never from decision rows."""

    async def test_round_trip_counts_once(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            for _ in range(2):  # two losing LLM round trips, as the agent records them
                entry = await _save_decision(storage, "BTC/USDT", action="buy")
                exit_ = await _save_decision(storage, "BTC/USDT", action="sell")
                await storage.set_realized_pnl(exit_, -5.0)  # the SELL decision's own row
                await storage.add_realized_pnl(entry, -5.0)  # FIFO share on the entry
                await _save_closing_fill(storage, "BTC/USDT", -5.0)

            engine = RiskEngine(risk_settings)
            await rehydrate_risk_engine(engine, storage)

            # Pre-§7.46: 4 rows with PnL → streak 4 ≥ 3 → phantom cooldown.
            assert engine._loss_tracker.consecutive_losses == 2
            assert not engine._loss_tracker.in_cooldown
        finally:
            await storage.close()

    async def test_legacy_history_falls_back_to_entry_decisions(
        self, risk_settings: RiskSettings, tmp_db_path: str
    ) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            for _ in range(3):  # pre-§7.46: no closing-fill outcomes were stored
                entry = await _save_decision(storage, "BTC/USDT", action="buy")
                await storage.add_realized_pnl(entry, -2.0)
                exit_ = await _save_decision(storage, "BTC/USDT", action="sell")
                await storage.set_realized_pnl(exit_, -2.0)

            engine = RiskEngine(risk_settings)
            await rehydrate_risk_engine(engine, storage)
            assert engine._loss_tracker.consecutive_losses == 3  # entries only, not 6
        finally:
            await storage.close()


class TestLiveOutcomeCoverage:
    """§7.46: every closing path feeds the loss streak — close-all included."""

    async def test_close_all_records_the_outcome(self, risk_settings: RiskSettings) -> None:
        from unittest.mock import AsyncMock

        from src.core.decision_pipeline import DecisionPipeline

        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, 100.0)
        executor.update_price("BTC/USDT", 90.0)
        engine = RiskEngine(risk_settings)
        pipeline = DecisionPipeline(
            provider=AsyncMock(), llm_client=AsyncMock(), risk_engine=engine, executor=executor
        )

        await pipeline.close_all_positions()

        assert engine._loss_tracker.consecutive_losses == 1


# ── §7.58: venue executors (Kraken/XTB) ────────────────────────


def _spot_kraken() -> tuple[CcxtExecutor, AsyncMock]:
    client = AsyncMock()
    client.fetch_positions.side_effect = Exception("kraken fetchPositions() not supported")
    client.fetch_balance.return_value = {"total": {"BTC": 10.0, "ETH": 10.0}}
    return CcxtExecutor(client, quote_currency="EUR", venue="test"), client


async def _entry_decision(storage: Storage, symbol: str, sl: float, tp: float) -> int:
    return await storage.save_llm_decision(
        symbol=symbol,
        action="buy",
        confidence=0.9,
        reasoning="entry",
        stop_loss=sl,
        take_profit=tp,
        risk_verdict="approved",
        risk_reason=None,
    )


async def _fill(
    storage: Storage,
    order_id: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    decision_id: int | None = None,
    status: str = "filled",
) -> None:
    await storage.save_order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        status=status,
        decision_id=decision_id,
    )


class TestVenueRehydration:
    """§7.58: FIFO ledger, exit levels and pending orders survive a venue restart."""

    async def test_kraken_spot_book_levels_and_attribution_survive(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            d1 = await _entry_decision(storage, "BTC/USDT", 90.0, 150.0)
            d2 = await _entry_decision(storage, "BTC/USDT", 95.0, 160.0)
            await _fill(storage, "K1", "BTC/USDT", "buy", 1.0, 100.0, d1)
            await _fill(storage, "K2", "BTC/USDT", "buy", 1.0, 110.0, d2)
            await _fill(storage, "K3", "BTC/USDT", "sell", 0.5, 120.0)
            # A paper-era fill of the same agent never happened at the venue.
            await _fill(storage, "paper-abc", "BTC/USDT", "buy", 5.0, 1.0, d1)

            executor, client = _spot_kraken()
            await rehydrate_from_storage(RiskEngine(RiskSettings()), executor, storage)

            (pos,) = await executor.get_positions()
            assert pos.quantity == pytest.approx(1.5)
            assert pos.avg_entry_price == pytest.approx((0.5 * 100.0 + 1.0 * 110.0) / 1.5)
            # The latest entry's plan is enforced again (live rule: last buy wins).
            assert (pos.stop_loss, pos.take_profit) == (95.0, 160.0)

            client.create_order.return_value = {
                "id": "K4",
                "status": "closed",
                "average": 130.0,
                "filled": 1.5,
            }
            sell = await executor.place_order("BTC/USDT", OrderSide.SELL, 1.5, price=130.0)
            assert sell.realized_pnl == pytest.approx(0.5 * 30.0 + 1.0 * 20.0)
            entries = {e.entry_decision_id: e.pnl for e in sell.closed_entries}
            assert entries == {d1: pytest.approx(15.0), d2: pytest.approx(20.0)}
            assert executor._exit_levels == {}
        finally:
            await storage.close()

    async def test_flat_symbol_has_no_lots_or_levels(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            d1 = await _entry_decision(storage, "ETH/USDT", 9.0, 15.0)
            await _fill(storage, "E1", "ETH/USDT", "buy", 2.0, 10.0, d1)
            await _fill(storage, "E2", "ETH/USDT", "sell", 2.0, 12.0)

            executor, _ = _spot_kraken()
            await rehydrate_venue_executor(executor, storage)
            assert await executor.get_positions() == []
            assert executor._exit_levels == {}
        finally:
            await storage.close()

    async def test_only_own_agent_fills_are_replayed(self, tmp_db_path: str) -> None:
        other = Storage(tmp_db_path, agent="stocks")
        await other.initialize()
        await _fill(other, "X1", "BTC/USDT", "buy", 1.0, 100.0)
        await other.close()

        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            executor, _ = _spot_kraken()
            await rehydrate_venue_executor(executor, storage)
            assert await executor.get_positions() == []
        finally:
            await storage.close()

    async def test_pending_order_is_reconciled_after_restart(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            d1 = await _entry_decision(storage, "BTC/USDT", 90.0, 150.0)
            await _fill(storage, "OPEN-1", "BTC/USDT", "buy", 1.0, 100.0, d1, status="pending")

            executor, client = _spot_kraken()
            await rehydrate_venue_executor(executor, storage)

            client.fetch_order.return_value = {
                "id": "OPEN-1",
                "status": "closed",
                "average": 101.0,
                "filled": 1.0,
            }
            (update,) = await executor.reconcile_open_orders()
            client.fetch_order.assert_awaited_once_with("OPEN-1", "BTC/USDT")
            assert (update.status, update.price) == ("filled", pytest.approx(101.0))
            # The late fill entered the ledger with its entry decision + plan.
            (pos,) = await executor.get_positions()
            assert (pos.quantity, pos.stop_loss, pos.take_profit) == (1.0, 90.0, 150.0)
            assert executor.pending_decision_id("OPEN-1") == d1
            # And it is cancellable (symbol known again).
            assert "OPEN-1" in executor._order_symbols
        finally:
            await storage.close()

    async def test_xtb_levels_reattach_to_venue_positions(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="stocks")
        await storage.initialize()
        try:
            d1 = await _entry_decision(storage, "AAPL", 95.0, 130.0)
            await _fill(storage, "101", "AAPL", "buy", 2.0, 100.0, d1)

            client = AsyncMock()
            client.get_positions.return_value = [
                {"symbol": "AAPL", "quantity": 2.0, "avg_entry_price": 100.0}
            ]
            executor = XTBExecutor(client)
            await rehydrate_from_storage(RiskEngine(RiskSettings()), executor, storage)

            (pos,) = await executor.get_positions()
            assert (pos.stop_loss, pos.take_profit) == (95.0, 130.0)

            client.get_open_trades.return_value = [
                {"order": 101, "symbol": "AAPL", "cmd": 0, "volume": 2.0}
            ]
            client.close_trade.return_value = {"order_id": "102", "status": "filled"}
            sell = await executor.place_order("AAPL", OrderSide.SELL, 2.0, price=90.0)
            assert sell.realized_pnl == pytest.approx(-20.0)
            assert [e.entry_decision_id for e in sell.closed_entries] == [d1]
        finally:
            await storage.close()

    async def test_storage_failure_is_fail_soft(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            executor, _ = _spot_kraken()
            with (
                patch.object(storage, "get_filled_orders", side_effect=RuntimeError("db gone")),
                patch.object(storage, "get_pending_orders", side_effect=RuntimeError("db gone")),
            ):
                await rehydrate_venue_executor(executor, storage)  # must not raise
            assert await executor.get_positions() == []
            assert executor._open_orders == {}
        finally:
            await storage.close()

    async def test_paper_executor_is_untouched(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            executor = PaperExecutor(initial_cash=1_000.0)
            await rehydrate_venue_executor(executor, storage)  # no hooks → no-op
            assert executor.cash == pytest.approx(1_000.0)
        finally:
            await storage.close()


class TestVenueSwitches:
    """§7.61: an agent switched between executors never restores the other's history."""

    async def test_venue_to_paper_restores_the_paper_book(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            storage.bind_venue("paper")
            await _fill(storage, "paper-1", "BTC/USDT", "buy", 1.0, 100.0)
            held = Position(
                symbol="BTC/USDT", quantity=1.0, avg_entry_price=100.0, current_price=100.0
            )
            await storage.save_portfolio_snapshot(
                cash=900.0, positions_json=_positions_json(held), total_value=1_000.0
            )
            storage.bind_venue("kraken-live")
            await _fill(storage, "K-1", "ETH/USDT", "buy", 3.0, 10.0)
            await storage.save_portfolio_snapshot(cash=12.0, positions_json="[]", total_value=42.0)

            paper = PaperExecutor(initial_cash=5.0, slippage_pct=0.0)
            assert await rehydrate_paper_executor(paper, storage) is True
            assert paper.cash == pytest.approx(900.0)
            assert [p.symbol for p in await paper.get_positions()] == ["BTC/USDT"]
            assert paper._tracker.quantity("ETH/USDT") == 0.0  # venue fill not replayed
        finally:
            await storage.close()

    async def test_sandbox_fills_never_reach_the_live_ledger(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            storage.bind_venue("binance-sandbox")
            await _fill(storage, "S-1", "BTC/USDT", "buy", 1.0, 100.0)
            await _fill(storage, "S-2", "BTC/USDT", "buy", 1.0, 100.0, status="pending")

            client = AsyncMock()
            client.fetch_positions.side_effect = Exception("not supported")
            client.fetch_balance.return_value = {"total": {"BTC": 5.0}}
            live = CcxtExecutor(client, venue="binance-live", quote_currency="EUR")
            await rehydrate_venue_executor(live, storage)
            assert await live.get_positions() == []
            assert live._open_orders == {}
        finally:
            await storage.close()

    def test_executor_venue_only_accepts_labels(self) -> None:
        assert executor_venue(PaperExecutor()) == "paper"
        assert executor_venue(object()) is None
        assert executor_venue(AsyncMock()) is None  # mocks never stamp rows
