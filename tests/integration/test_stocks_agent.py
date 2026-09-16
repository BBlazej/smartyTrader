"""Integration tests for the stocks agent — full pipeline with mocked components.

Exercises the agent's lifecycle (start → cycle → shutdown), the market-hours
guard, and persistence against a real :class:`DecisionPipeline` (only provider +
LLM mocked), a real paper executor and a real (temp) SQLite storage layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.stocks_agent import StocksAgent
from src.core.config import RiskSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import OHLCV, MarketSnapshot, TradeSignal
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor

SYMBOL = "AAPL"


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


def make_snapshot(price: float = 160.0) -> MarketSnapshot:
    candles = [
        OHLCV(open=price, high=price * 1.01, low=price * 0.99, close=price, volume=1_000.0)
        for _ in range(5)
    ]
    return MarketSnapshot(symbol=SYMBOL, timeframe="1d", candles=candles)


def make_provider(prices: list[float]) -> MagicMock:
    provider = MagicMock()
    provider.fetch_snapshot = AsyncMock(side_effect=[make_snapshot(p) for p in prices])
    return provider


def make_llm(signals: list[TradeSignal]) -> AsyncMock:
    client = AsyncMock()
    client.ask_trade_signal = AsyncMock(side_effect=signals)
    return client


def buy_signal() -> TradeSignal:
    return TradeSignal(
        symbol=SYMBOL,
        action="buy",
        confidence=0.85,
        reasoning="momentum",
        stop_loss=150.0,
        take_profit=180.0,
    )


def sell_signal() -> TradeSignal:
    return TradeSignal(
        symbol=SYMBOL,
        action="sell",
        confidence=0.8,
        reasoning="take profit",
        stop_loss=150.0,  # required by the risk gate on any active signal
    )


@pytest.fixture()
def provider() -> MagicMock:
    return make_provider([160.0])


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
    """Real pipeline; only provider + LLM mocked. The pipeline persists each
    decision itself right after the risk gate (§7.8)."""
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
    market_hours: str = "24h",
) -> StocksAgent:
    llm_client = AsyncMock()
    return StocksAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=risk_engine,
        llm_client=llm_client,
        symbols=[SYMBOL],
        timeframe="1d",
        market_hours=market_hours,
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


class TestMarketHoursGuard:
    async def test_cycle_skipped_when_market_closed(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        # A window that never contains the current wall clock → cycle is skipped.
        agent = make_agent(
            pipeline, storage, risk_engine, paper_executor, market_hours="00:00-00:00"
        )
        results = await agent.run_cycle()
        assert results == []
        # Nothing persisted — the pipeline never ran.
        assert await storage.get_recent_decisions() == []
        assert await storage.get_recent_orders() == []

    async def test_cycle_runs_when_market_open(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        # "24h" is a no-op window → always open.
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        results = await agent.run_cycle()
        assert len(results) == 1
        assert await storage.get_recent_decisions() != []


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
        assert results[0].decision_id == decisions[0].id

    async def test_cycle_persists_order(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        results = await agent.run_cycle()

        orders = await storage.get_recent_orders()
        assert len(orders) == 1
        assert orders[0].side == "buy"
        assert orders[0].status == "filled"
        # The order links back to the decision that produced it (§7.8).
        assert orders[0].decision_id == results[0].decision_id

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

        assert risk_engine._daily_tracker.start_of_day_value is not None
        assert risk_engine._daily_tracker.daily_pnl_pct == pytest.approx(0.0)


class TestRealizedPnlBackfill:
    """Buy → sell across cycles must stamp realized PnL on the sell decision and
    back onto the entry decision via the FIFO tracker's closed_entries (§7.8)."""

    async def test_closing_sell_backfills_entry_and_sell_decisions(
        self,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        provider = make_provider([160.0, 180.0])
        llm = make_llm([buy_signal(), sell_signal()])
        pipeline = DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=risk_engine,
            executor=paper_executor,
            storage=storage,
        )
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)

        first = (await agent.run_cycle())[0]
        second = (await agent.run_cycle())[0]

        assert second.executed
        order = second.order_result
        assert order is not None
        assert order.realized_pnl is not None and order.realized_pnl > 0
        assert [e.entry_decision_id for e in order.closed_entries] == [first.decision_id]

        decisions = {d.id: d for d in await storage.get_recent_decisions()}
        assert decisions[second.decision_id].realized_pnl == pytest.approx(order.realized_pnl)
        assert decisions[first.decision_id].realized_pnl == pytest.approx(order.realized_pnl)


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

        results = await agent.run_cycle()
        assert results == []
