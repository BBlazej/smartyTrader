"""Fail-soft, lossless post-order persistence (§7.44, external review 4 H2).

The order has already executed when the agent persists it, so a storage error there
must neither abort the cycle / skip the heartbeat nor lose the record. Real SQLite +
real pipeline + paper executor; storage methods are made flaky on purpose.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from src.agents.crypto_agent import CryptoAgent
from src.core.config import RiskSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import OHLCV, ClosedEntry, MarketSnapshot, OrderResult, OrderSide, TradeSignal
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.ccxt_executor import CcxtExecutor
from src.execution.paper_executor import PaperExecutor


def _snapshot(symbol: str) -> MarketSnapshot:
    candles = [OHLCV(open=100, high=101, low=99, close=100, volume=1) for _ in range(5)]
    return MarketSnapshot(symbol=symbol, timeframe="1h", candles=candles)


def _buy(symbol: str) -> TradeSignal:
    return TradeSignal(
        symbol=symbol, action="buy", confidence=0.9, reasoning="entry", stop_loss=90.0
    )


def _agent(storage: Storage, executor, symbols: list[str]) -> CryptoAgent:
    provider = MagicMock()
    provider.fetch_snapshot = AsyncMock(side_effect=lambda s, tf: _snapshot(s))
    llm = AsyncMock()
    llm.ask_trade_signal = AsyncMock(side_effect=lambda **k: _buy("X"))
    pipeline = DecisionPipeline(
        provider=provider,
        llm_client=llm,
        risk_engine=RiskEngine(RiskSettings()),
        executor=executor,
        storage=storage,
    )
    agent = CryptoAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=pipeline.risk_engine,
        llm_client=llm,
        pairs=symbols,
    )
    agent._persist_retry_delays = (0.0, 0.0)
    return agent


@pytest.fixture()
async def storage(tmp_path):
    store = Storage(str(tmp_path / "resilience.db"), agent="crypto")
    await store.initialize()
    yield store
    await store.close()


class TestOrderRowPersistence:
    async def test_transient_failure_is_retried(self, storage: Storage) -> None:
        real = storage.save_order

        async def flaky(**row):
            if not getattr(flaky, "failed", False):
                flaky.failed = True
                raise RuntimeError("database is locked")
            return await real(**row)

        storage.save_order = flaky
        agent = _agent(
            storage, PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0), ["BTC/USDT"]
        )

        results = await agent.run_cycle()

        assert results[0].executed
        assert [o.symbol for o in await storage.get_filled_orders()] == ["BTC/USDT"]

    async def test_permanent_failure_never_aborts_the_cycle(self, storage: Storage) -> None:
        storage.save_order = AsyncMock(side_effect=RuntimeError("disk full"))
        agent = _agent(
            storage,
            PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0),
            ["BTC/USDT", "ETH/USDT"],
        )
        agent._alerts.send = AsyncMock()

        with capture_logs() as logs:
            results = await agent.run_cycle()

        # Both symbols ran (pre-§7.44 the first failure aborted the loop).
        assert [r.symbol for r in results] == ["BTC/USDT", "ETH/USDT"]
        assert storage.save_order.await_count == 6  # 3 attempts × 2 orders
        audit = [e for e in logs if e["event"] == "order_persist_failed"]
        assert {e["symbol"] for e in audit} == {"BTC/USDT", "ETH/USDT"}
        assert all(e["status"] == "filled" and e["order_id"] for e in audit)
        assert any(c.args[0] == "error" for c in agent._alerts.send.await_args_list)
        # The heartbeat still landed.
        assert (await storage.get_agent_control("crypto")).last_cycle_at is not None

    async def test_portfolio_snapshot_failure_is_contained(self, storage: Storage) -> None:
        storage.save_portfolio_snapshot = AsyncMock(side_effect=RuntimeError("locked"))
        agent = _agent(
            storage, PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0), ["BTC/USDT"]
        )

        results = await agent.run_cycle()

        assert results[0].executed
        assert len(await storage.get_filled_orders()) == 1
        assert (await storage.get_agent_control("crypto")).last_cycle_at is not None

    async def test_unexpected_post_process_error_is_contained(self, storage: Storage) -> None:
        agent = _agent(
            storage,
            PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0),
            ["BTC/USDT", "ETH/USDT"],
        )
        agent._maybe_alert = AsyncMock(side_effect=RuntimeError("boom"))

        results = await agent.run_cycle()

        assert len(results) == 2
        assert (await storage.get_agent_control("crypto")).last_cycle_at is not None


class TestReconciliationIsLossless:
    """A reconciled venue fill is confirmed to the executor only once persisted."""

    @staticmethod
    async def _pending_kraken(storage: Storage, decision_id: int | None = None) -> CcxtExecutor:
        client = AsyncMock()
        client.create_order.return_value = {"id": "V-1", "status": "open"}
        client.fetch_free_balance.return_value = {"USDT": {"free": 1_000.0}}
        client.fetch_positions.return_value = []
        executor = CcxtExecutor(client, quote_currency="EUR", venue="test")
        await executor.place_order(
            "BTC/USDT", OrderSide.BUY, 1.0, price=100.0, decision_id=decision_id
        )
        client.fetch_order.return_value = {
            "id": "V-1",
            "status": "closed",
            "average": 101.0,
            "filled": 1.0,
            "updated": 1_700_000_000_000,
        }
        return executor

    async def test_failed_status_write_is_redelivered_next_cycle(self, storage: Storage) -> None:
        await storage.save_order("V-1", "BTC/USDT", "buy", 1.0, 100.0, "pending")
        executor = await self._pending_kraken(storage)
        agent = _agent(storage, executor, [])

        real_update = storage.update_order_status
        storage.update_order_status = AsyncMock(side_effect=RuntimeError("database is locked"))
        await agent._reconcile_orders()
        assert (await storage.get_recent_orders())[0].status == "pending"

        storage.update_order_status = real_update
        await agent._reconcile_orders()
        row = (await storage.get_recent_orders())[0]
        assert row.status == "filled" and row.price == pytest.approx(101.0)
        # Confirmed → no third delivery; the venue was polled exactly once.
        assert await executor.reconcile_open_orders() == []
        executor.client.fetch_order.assert_awaited_once()

    async def test_lost_row_is_recreated_with_its_decision(self, storage: Storage) -> None:
        decision_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="entry",
            stop_loss=90.0,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        # No stored order row at all — its write failed when the order was placed.
        executor = await self._pending_kraken(storage, decision_id=decision_id)
        agent = _agent(storage, executor, [])

        await agent._reconcile_orders()

        rows = await storage.get_filled_orders()
        assert [(r.order_id, r.decision_id) for r in rows] == [("V-1", decision_id)]

    async def test_attribution_is_applied_once(self, storage: Storage) -> None:
        entry = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="entry",
            stop_loss=90.0,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        await storage.save_order("V-2", "BTC/USDT", "sell", 1.0, 100.0, "pending")
        executor = MagicMock()
        closing = OrderResult(
            order_id="V-2",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            quantity=1.0,
            price=110.0,
            status="filled",
            realized_pnl=10.0,
            closed_entries=[ClosedEntry(entry_decision_id=entry, pnl=10.0)],
        )
        confirmed: set[str] = set()
        executor.reconcile_open_orders = AsyncMock(
            side_effect=lambda: [] if "V-2" in confirmed else [closing]
        )
        executor.confirm_reconciled = lambda oid: confirmed.add(oid)
        agent = _agent(storage, executor, [])
        agent._pipeline.get_portfolio_state = AsyncMock(
            return_value=MagicMock(cash=0.0, positions=[], total_value=0.0, unrealized_pnl=0.0)
        )

        await agent._reconcile_orders()
        await agent._reconcile_orders()

        closed = await storage.get_closed_decisions()
        assert [(d.id, d.realized_pnl) for d in closed] == [(entry, pytest.approx(10.0))]


class TestClosingFillOutcomes:
    """§7.46: closing fills persist their realized PnL; reconciled fills feed the streak."""

    async def test_losing_round_trip_rehydrates_as_one_loss(self, storage: Storage) -> None:
        from src.core.rehydration import rehydrate_risk_engine

        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        agent = _agent(storage, executor, ["BTC/USDT"])
        await agent.run_cycle()  # BUY at 100
        lower = [OHLCV(open=95, high=95, low=95, close=95, volume=1) for _ in range(5)]
        agent._pipeline.provider.fetch_snapshot = AsyncMock(
            return_value=MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=lower)
        )
        agent._pipeline.llm_client.ask_trade_signal = AsyncMock(
            return_value=TradeSignal(
                symbol="BTC/USDT", action="sell", confidence=0.9, reasoning="cut the loss"
            )
        )
        await agent.run_cycle()  # SELL at 95 → loss

        fills = await storage.get_recent_closing_fills()
        assert len(fills) == 1 and fills[0].realized_pnl < 0
        assert agent._risk_engine._loss_tracker.consecutive_losses == 1

        restarted = RiskEngine(RiskSettings())
        await rehydrate_risk_engine(restarted, storage)
        assert restarted._loss_tracker.consecutive_losses == 1  # was 2 pre-§7.46

    async def test_reconciled_closing_fill_feeds_the_streak(self, storage: Storage) -> None:
        await storage.save_order("V-9", "BTC/USDT", "sell", 1.0, 100.0, "pending")
        losing = OrderResult(
            order_id="V-9",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            quantity=1.0,
            price=90.0,
            status="filled",
            realized_pnl=-10.0,
        )
        executor = MagicMock()
        executor.reconcile_open_orders = AsyncMock(return_value=[losing])
        executor.confirm_reconciled = MagicMock()
        agent = _agent(storage, executor, [])
        agent._pipeline.get_portfolio_state = AsyncMock(
            return_value=MagicMock(cash=0.0, positions=[], total_value=0.0, unrealized_pnl=0.0)
        )

        await agent._reconcile_orders()

        assert agent._risk_engine._loss_tracker.consecutive_losses == 1
        (row,) = await storage.get_recent_closing_fills()
        assert row.order_id == "V-9" and row.realized_pnl == pytest.approx(-10.0)
