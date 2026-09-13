"""Integration tests for the stocks agent — full pipeline with mocked components.

Exercises the agent's lifecycle (start → cycle → shutdown), the market-hours guard,
and persistence of decisions / orders / portfolio against a real (temp) SQLite storage
layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.stocks_agent import StocksAgent
from src.core.config import RiskSettings
from src.core.decision_pipeline import PipelineResult
from src.core.models import (
    MarketSnapshot,
    OrderResult,
    OrderSide,
    PortfolioState,
    RiskResult,
    RiskVerdict,
    TradeSignal,
)
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor


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


@pytest.fixture()
def pipeline(paper_executor: PaperExecutor, risk_engine: RiskEngine) -> MagicMock:
    """A stand-in pipeline whose run() returns a canned filled result."""
    filled_result = PipelineResult(
        symbol="AAPL",
        signal=TradeSignal(
            symbol="AAPL",
            action="buy",
            confidence=0.85,
            reasoning="momentum",
            stop_loss=150.0,
            take_profit=180.0,
        ),
        risk_result=RiskResult(verdict=RiskVerdict.APPROVED),
        order_result=OrderResult(
            order_id="paper-1",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=10.0,
            price=160.0,
            status="filled",
        ),
        snapshot=MarketSnapshot(symbol="AAPL", timeframe="1d", candles=[]),
    )
    mock = MagicMock()
    mock.run = AsyncMock(return_value=filled_result)
    mock._get_portfolio_state = AsyncMock(
        return_value=PortfolioState(cash=paper_executor.cash, positions=[])
    )
    return mock


def make_agent(
    pipeline: MagicMock,
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
        symbols=["AAPL"],
        timeframe="1d",
        market_hours=market_hours,
    )


class TestLifecycle:
    async def test_start_stop(
        self,
        pipeline: MagicMock,
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
        pipeline: MagicMock,
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
        pipeline: MagicMock,
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
        # Nothing persisted, no pipeline run.
        pipeline.run.assert_not_awaited()
        assert await storage.get_recent_decisions() == []

    async def test_cycle_runs_when_market_open(
        self,
        pipeline: MagicMock,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        # "24h" is a no-op window → always open.
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        results = await agent.run_cycle()
        assert len(results) == 1
        pipeline.run.assert_awaited_once()


class TestCycle:
    async def test_cycle_persists_decision(
        self,
        pipeline: MagicMock,
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

    async def test_cycle_persists_order(
        self,
        pipeline: MagicMock,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        await agent.run_cycle()

        orders = await storage.get_recent_orders()
        assert len(orders) == 1
        assert orders[0].order_id == "paper-1"
        assert orders[0].side == "buy"
        assert orders[0].status == "filled"

    async def test_cycle_persists_portfolio(
        self,
        pipeline: MagicMock,
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
        pipeline: MagicMock,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)
        await agent.run_cycle()

        assert risk_engine._daily_tracker.start_of_day_value is not None
        assert risk_engine._daily_tracker.daily_pnl_pct == pytest.approx(0.0)


class TestCycleErrorIsolation:
    async def test_pipeline_exception_does_not_break_cycle(
        self,
        pipeline: MagicMock,
        storage: Storage,
        risk_engine: RiskEngine,
        paper_executor: PaperExecutor,
    ) -> None:
        pipeline.run = AsyncMock(side_effect=RuntimeError("boom"))
        agent = make_agent(pipeline, storage, risk_engine, paper_executor)

        results = await agent.run_cycle()
        assert results == []
