"""Integration tests for the crypto agent — full pipeline with mocked components.

These exercise the agent's lifecycle (start → cycle → shutdown) against a real
:class:`DecisionPipeline` (only the market-data provider and the LLM are mocked)
plus a real paper executor and a real (temp) SQLite storage layer, so decision
persistence, order↔decision links and the realized-PnL backfill all run for real.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.crypto_agent import CryptoAgent
from src.core.config import RiskSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import (
    OHLCV,
    MarketSnapshot,
    TradeSignal,
)
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor

SYMBOL = "BTC/USDT"


@pytest.fixture()
def risk_engine() -> RiskEngine:
    return RiskEngine(
        RiskSettings(
            max_position_pct=0.10,
            daily_loss_limit_pct=0.02,
            max_drawdown_pct=0.05,
            consecutive_losses_cooldown_minutes=60,
            max_open_positions=5,
            min_confidence=0.6,
        )
    )


@pytest.fixture()
async def storage(tmp_db_path: str) -> Storage:
    store = Storage(tmp_db_path)
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture()
def paper_executor() -> PaperExecutor:
    return PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)


def make_snapshot(price: float = 100.0) -> MarketSnapshot:
    """A flat little candle series around ``price`` (enough for indicators)."""
    candles = [
        OHLCV(open=price, high=price * 1.01, low=price * 0.99, close=price, volume=10.0)
        for _ in range(5)
    ]
    return MarketSnapshot(symbol=SYMBOL, timeframe="1h", candles=candles)


def make_provider(prices: list[float]) -> MagicMock:
    """Provider stand-in serving one snapshot per requested cycle price."""
    provider = MagicMock()
    provider.fetch_snapshot = AsyncMock(side_effect=[make_snapshot(p) for p in prices])
    return provider


def make_llm(signals: list[TradeSignal]) -> AsyncMock:
    """LLM stand-in returning the given signals, one per call."""
    client = AsyncMock()
    client.ask_trade_signal = AsyncMock(side_effect=signals)
    return client


def buy_signal() -> TradeSignal:
    return TradeSignal(
        symbol=SYMBOL,
        action="buy",
        confidence=0.85,
        reasoning="momentum",
        stop_loss=95.0,
        take_profit=110.0,
    )


def sell_signal() -> TradeSignal:
    return TradeSignal(
        symbol=SYMBOL,
        action="sell",
        confidence=0.8,
        reasoning="take profit",
        stop_loss=95.0,  # required by the risk gate on any active signal
    )


@pytest.fixture()
def provider() -> MagicMock:
    return make_provider([100.0])


@pytest.fixture()
def llm_client() -> AsyncMock:
    return make_llm([buy_signal()])


@pytest.fixture()
def pipeline(
    provider: MagicMock,
    llm_client: AsyncMock,
    risk_engine: RiskEngine,
    paper_executor: PaperExecutor,
    storage: Storage,
) -> DecisionPipeline:
    """Real pipeline; only provider + LLM are mocked. The pipeline itself now
    persists each decision right after the risk gate (§7.8), so it needs the
    real storage to produce order-linkable decision ids."""
    return DecisionPipeline(
        provider=provider,
        llm_client=llm_client,
        risk_engine=risk_engine,
        executor=paper_executor,
        storage=storage,
    )


def make_agent(
    pipeline: DecisionPipeline,
    storage: Storage,
    risk_engine: RiskEngine,
    paper_executor: PaperExecutor,
) -> CryptoAgent:
    llm_client = AsyncMock()
    return CryptoAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=risk_engine,
        llm_client=llm_client,
        pairs=[SYMBOL],
    )


class TestLifecycle:
    async def test_start_stop(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        assert agent.running is False
        await agent.start()
        assert agent.running is True
        await agent.stop()
        assert agent.running is False

    async def test_shutdown_closes_llm(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        llm = agent._llm_client
        await agent.shutdown()
        llm.close.assert_awaited_once()
        assert agent.running is False


class TestCycle:
    async def test_cycle_persists_decision(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        results = await agent.run_cycle()

        assert len(results) == 1
        decisions = await storage.get_recent_decisions()
        assert len(decisions) == 1
        assert decisions[0].action == "buy"
        assert decisions[0].risk_verdict == "approved"
        # The result carries the row id of the decision the pipeline persisted.
        assert results[0].decision_id == decisions[0].id

    async def test_cycle_persists_order(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        await agent.run_cycle()

        orders = await storage.get_recent_orders()
        assert len(orders) == 1
        assert orders[0].side == "buy"
        assert orders[0].status == "filled"
        # The order links back to the decision that produced it (§7.8).
        assert orders[0].decision_id is not None

    async def test_cycle_persists_portfolio(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        await agent.run_cycle()

        snapshot = await storage.get_latest_portfolio_snapshot()
        assert snapshot is not None

    async def test_cycle_updates_daily_value(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        await agent.run_cycle()

        # The daily-loss baseline must have been set from the portfolio value.
        assert risk_engine._daily_tracker.start_of_day_value is not None
        assert risk_engine._daily_tracker.daily_pnl_pct == pytest.approx(0.0)


class TestRealizedPnlBackfill:
    """Closing a position must stamp realized PnL onto the sell decision *and*
    back to the entry decision that opened the lot (the "learn from its own
    track record" loop, §7.8)."""

    async def _run_buy_then_sell(
        self,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ):
        # Cycle 2 sits between the entry signal's stop (95) and take-profit (110)
        # so the LLM-driven close — not the §7.9 auto-exit — is what runs here.
        provider = make_provider([100.0, 105.0])
        llm = make_llm([buy_signal(), sell_signal()])
        pipeline = DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=risk_engine,
            executor=paper_executor,
            storage=storage,
        )
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)

        first = await agent.run_cycle()
        second = await agent.run_cycle()
        return first[0], second[0]

    async def test_buy_does_not_backfill_realized_pnl(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        # A buy (no realized_pnl on the order) must leave the decision outcome
        # as None (position still open), not fabricate a PnL.
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        await agent.run_cycle()

        decisions = await storage.get_recent_decisions()
        assert len(decisions) == 1
        assert decisions[0].realized_pnl is None

    async def test_closing_sell_backfills_entry_and_sell_decisions(
        self,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        first, second = await self._run_buy_then_sell(storage, risk_engine, paper_executor)

        assert second.executed
        order = second.order_result
        assert order is not None
        assert order.realized_pnl is not None and order.realized_pnl > 0
        # The FIFO tracker attributes the fill to the buy decision from cycle 1.
        assert [e.entry_decision_id for e in order.closed_entries] == [first.decision_id]

        decisions = {d.id: d for d in await storage.get_recent_decisions()}
        sell_row = decisions[second.decision_id]
        entry_row = decisions[first.decision_id]
        assert sell_row.action == "sell"
        assert sell_row.realized_pnl == pytest.approx(order.realized_pnl)
        # The originating buy decision now carries its share of the outcome too.
        assert entry_row.realized_pnl == pytest.approx(order.realized_pnl)

    async def test_orders_link_to_their_own_decisions(
        self,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        first, second = await self._run_buy_then_sell(storage, risk_engine, paper_executor)

        orders = {o.order_id: o for o in await storage.get_recent_orders()}
        buy_order = next(o for o in orders.values() if o.side == "buy")
        sell_order = next(o for o in orders.values() if o.side == "sell")
        assert buy_order.decision_id == first.decision_id
        assert sell_order.decision_id == second.decision_id


class TestAutoExit:
    """§7.9 end-to-end: a breached stop closes the position on the next cycle
    without an LLM call, and the outcome still lands on the entry decision."""

    async def test_stop_loss_exit_persisted_and_backfilled(
        self,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        provider = make_provider([100.0, 90.0])  # cycle 2 sits below the 95 stop
        llm = make_llm([buy_signal()])  # a second LLM call would raise here
        pipeline = DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=risk_engine,
            executor=paper_executor,
            storage=storage,
        )
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)

        first = (await agent.run_cycle())[0]
        assert first.decision_id is not None

        results = await agent.run_cycle()
        assert len(results) == 1
        second = results[0]
        assert second.auto_exit is True
        assert second.exit_reason == "stop_loss"
        assert second.signal is None

        # The exit order is on record, linked to no decision (the LLM never acted).
        orders = {o.side: o for o in await storage.get_recent_orders()}
        assert orders["sell"].decision_id is None

        # Still exactly one decision row — and it carries the closed outcome.
        decisions = {d.id: d for d in await storage.get_recent_decisions()}
        assert len(decisions) == 1
        entry = decisions[first.decision_id]
        assert entry.realized_pnl is not None
        assert entry.realized_pnl < 0


class TestCycleErrorIsolation:
    async def test_pipeline_exception_does_not_break_cycle(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        pipeline.run = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)

        # Should not raise; returns no results.
        results = await agent.run_cycle()
        assert results == []


class TestOrderReconciliation:
    """§7.28: the agent polls venue orders left pending once per cycle and
    persists their status transitions (fill data + entry attribution)."""

    class VenueLikeExecutor(PaperExecutor):
        """Paper book plus the venue-style ``reconcile_open_orders`` hook."""

        def __init__(self) -> None:
            super().__init__(initial_cash=10_000.0, slippage_pct=0.0)
            self.staged: list = []
            self.calls = 0

        async def reconcile_open_orders(self) -> list:
            self.calls += 1
            updates, self.staged = list(self.staged), []
            return updates

    async def test_pending_venue_order_is_reconciled_per_cycle(
        self,
        storage: Storage,
        risk_engine: RiskEngine,
    ) -> None:
        from src.core.models import ClosedEntry, OrderResult, OrderSide

        executor = self.VenueLikeExecutor()
        # A venue order we recorded as pending last cycle + the entry decision
        # its eventual fill must be attributed back to (§7.8).
        await storage.save_order(
            order_id="venue-1",
            symbol=SYMBOL,
            side="buy",
            quantity=1.0,
            price=99.0,
            status="pending",
        )
        entry_id = await storage.save_llm_decision(
            symbol=SYMBOL,
            action="buy",
            confidence=0.8,
            reasoning="earlier cycle",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        executor.staged.append(
            OrderResult(
                order_id="venue-1",
                symbol=SYMBOL,
                side=OrderSide.BUY,
                quantity=1.0,
                price=100.0,
                status="filled",
                realized_pnl=5.0,
                closed_entries=[ClosedEntry(entry_decision_id=entry_id, pnl=5.0)],
            )
        )

        llm = make_llm(
            [TradeSignal(symbol=SYMBOL, action="hold", confidence=0.5, reasoning="wait")]
        )
        pipeline = DecisionPipeline(
            provider=make_provider([100.0]),
            llm_client=llm,
            risk_engine=risk_engine,
            executor=executor,
            storage=storage,
        )
        agent = CryptoAgent(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=AsyncMock(),
            pairs=[SYMBOL],
        )

        await agent.run_cycle()

        assert executor.calls == 1  # exactly one poll per cycle
        order = next(o for o in await storage.get_recent_orders() if o.order_id == "venue-1")
        assert order.status == "filled"
        assert order.price == pytest.approx(100.0)
        assert order.filled_at is not None

        decisions = {d.id: d for d in await storage.get_closed_decisions()}
        assert decisions[entry_id].realized_pnl == pytest.approx(5.0)

    async def test_partial_fill_quantity_replaces_requested_size(
        self,
        storage: Storage,
        risk_engine: RiskEngine,
    ) -> None:
        """§7.61: the stored row carries the *filled* amount — the §7.58 replay source."""
        from src.core.models import OrderResult, OrderSide

        executor = self.VenueLikeExecutor()
        await storage.save_order(
            order_id="venue-2",
            symbol=SYMBOL,
            side="buy",
            quantity=1.0,
            price=99.0,
            status="pending",
        )
        executor.staged.append(
            OrderResult(
                order_id="venue-2",
                symbol=SYMBOL,
                side=OrderSide.BUY,
                quantity=0.4,
                price=100.0,
                status="filled",
                reason="partially filled; remainder cancelled at the venue",
            )
        )
        pipeline = DecisionPipeline(
            provider=make_provider([100.0]),
            llm_client=make_llm(
                [TradeSignal(symbol=SYMBOL, action="hold", confidence=0.5, reasoning="wait")]
            ),
            risk_engine=risk_engine,
            executor=executor,
            storage=storage,
        )
        agent = CryptoAgent(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=AsyncMock(),
            pairs=[SYMBOL],
        )

        await agent.run_cycle()

        (row,) = await storage.get_filled_orders()
        assert (row.order_id, row.quantity) == ("venue-2", pytest.approx(0.4))
