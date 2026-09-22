"""End-to-end control-loop integration tests (§7.31).

The full loop for real: HTTP requests to the actual ``create_control_app``
FastAPI surface (in-process ASGI transport, same event loop as storage) drive
the ``agent_control`` rows that a **real** :class:`CryptoAgent` — real pipeline
(only provider + LLM mocked), real risk engine, real paper executor, real SQLite
— reads and executes on its next ``run_cycle``.

Regression context (nightly_finds #16): the agent subclasses once keyed their
control row as ``crypto_agent`` while every consumer used ``crypto``, so latches
written through this API were never read and heartbeats were never seen. These
tests pin both halves of that contract on one shared key.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.agents.crypto_agent import CryptoAgent
from src.core.config import RiskSettings, Settings
from src.core.control_api import create_control_app
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import OHLCV, MarketSnapshot, TradeSignal
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor

SYMBOL = "BTC/USDT"
AGENT = "crypto"


def _settings(tmp_path) -> Settings:
    config = tmp_path / "settings.yaml"
    config.write_text(
        """
llm: {endpoint: "http://localhost:1234/v1/chat/completions", model: m}
crypto_agent: {enabled: true, interval_minutes: 5, pairs: ["BTC/USDT"], decision_history_limit: 10}
stocks_agent: {enabled: false, interval_minutes: 60, symbols: ["AAPL"], market_hours: "08:00-22:00", decision_history_limit: 10}
risk: {max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}
storage: {database_path: "x.db"}
monitoring: {log_level: INFO}
"""
    )
    return Settings(str(config))


def _snapshot(price: float = 100.0) -> MarketSnapshot:
    candles = [
        OHLCV(open=price, high=price * 1.01, low=price * 0.99, close=price, volume=10.0)
        for _ in range(5)
    ]
    return MarketSnapshot(symbol=SYMBOL, timeframe="1h", candles=candles)


def _buy_signal() -> TradeSignal:
    return TradeSignal(
        symbol=SYMBOL,
        action="buy",
        confidence=0.85,
        reasoning="momentum",
        stop_loss=95.0,
        take_profit=110.0,
    )


class _Env:
    """Shared fixture bundle: storage + agent + control-API client."""

    def __init__(self, storage, agent, executor, client) -> None:
        self.storage = storage
        self.agent = agent
        self.executor = executor
        self.client = client


@pytest.fixture()
async def env(tmp_path):
    storage = Storage(str(tmp_path / "loop.db"))
    await storage.initialize()

    provider = MagicMock()
    provider.fetch_snapshot = AsyncMock(side_effect=lambda *a, **k: _snapshot(100.0))
    llm = AsyncMock()
    llm.ask_trade_signal = AsyncMock(side_effect=lambda *a, **k: _buy_signal())

    risk_engine = RiskEngine(
        RiskSettings(
            max_position_pct=0.10,
            daily_loss_limit_pct=0.02,
            max_drawdown_pct=0.05,
            consecutive_losses_cooldown_minutes=60,
            max_open_positions=5,
            min_confidence=0.6,
        )
    )
    executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
    pipeline = DecisionPipeline(
        provider=provider,
        llm_client=llm,
        risk_engine=risk_engine,
        executor=executor,
        storage=storage,
    )
    # The REAL subclass — its component name is the control-plane key under test.
    agent = CryptoAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=risk_engine,
        llm_client=llm,
        pairs=[SYMBOL],
    )

    app = create_control_app(
        storage=storage,
        agent_name=AGENT,  # what the runner passes as ``component``
        settings=_settings(tmp_path),
        get_positions=executor.get_positions,
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://control")
    yield _Env(storage, agent, executor, client)
    await client.aclose()
    await storage.close()


class TestPauseResumeLoop:
    async def test_pause_via_http_halts_cycles(self, env: _Env) -> None:
        # Baseline cycle through the real pipeline opens a position.
        results = await env.agent.run_cycle()
        assert len(results) == 1
        assert (await env.executor.get_positions()) != []

        resp = await env.client.post(f"/api/agents/{AGENT}/pause")
        assert resp.status_code == 200

        before = [d.id for d in await env.storage.get_recent_decisions()]
        results = await env.agent.run_cycle()
        assert results == []
        after = [d.id for d in await env.storage.get_recent_decisions()]
        assert after == before  # nothing new ran

    async def test_heartbeat_is_visible_on_the_same_row_the_api_reads(self, env: _Env) -> None:
        """nightly_finds #16 regression: agent heartbeat ↔ control-API status key."""
        await env.agent.run_cycle()
        status = (await env.client.get(f"/api/agents/{AGENT}")).json()
        assert status["last_cycle_at"] is not None  # stale-free only if keys match

        # And while paused, the heartbeat keeps beating so liveness stays honest.
        await env.client.post(f"/api/agents/{AGENT}/pause")
        await env.agent.run_cycle()
        paused = (await env.client.get(f"/api/agents/{AGENT}")).json()
        assert paused["state"] == "paused"
        assert paused["last_cycle_at"] is not None

    async def test_resume_via_http_restarts_cycles(self, env: _Env) -> None:
        await env.client.post(f"/api/agents/{AGENT}/pause")
        assert await env.agent.run_cycle() == []

        resp = await env.client.post(f"/api/agents/{AGENT}/resume")
        assert resp.status_code == 200
        results = await env.agent.run_cycle()
        assert len(results) == 1


class TestCloseAllLoop:
    async def test_close_all_via_http_closes_positions_on_next_cycle(self, env: _Env) -> None:
        first = (await env.agent.run_cycle())[0]
        positions = await env.executor.get_positions()
        assert len(positions) == 1

        # Pause first so the *next* cycle only carries out the close (an un-paused
        # cycle would trade again right after closing — by design).
        await env.client.post(f"/api/agents/{AGENT}/pause")
        resp = await env.client.post(f"/api/agents/{AGENT}/close-all")
        assert resp.status_code == 200
        assert (await env.storage.get_agent_control(AGENT)).close_all_requested is True

        results = await env.agent.run_cycle()
        assert results == []  # close-all bypasses the decision pipeline results
        assert await env.executor.get_positions() == []

        # Latch cleared after execution — no repeat-close on every later cycle.
        control = await env.storage.get_agent_control(AGENT)
        assert control.close_all_requested is False

        # The closing sell is persisted and its outcome backfilled to the entry row.
        orders = {o.side: o for o in await env.storage.get_recent_orders()}
        assert orders["sell"].status == "filled"
        entry = next(d for d in await storage_decisions(env) if d.id == first.decision_id)
        assert entry.realized_pnl is not None

    async def test_close_all_executes_even_while_paused(self, env: _Env) -> None:
        await env.agent.run_cycle()
        assert (await env.executor.get_positions()) != []

        await env.client.post(f"/api/agents/{AGENT}/pause")
        await env.client.post(f"/api/agents/{AGENT}/close-all")

        results = await env.agent.run_cycle()
        assert results == []
        # Closes only reduce exposure — the pause latch must not strand the position.
        assert await env.executor.get_positions() == []


async def storage_decisions(env: _Env):
    return await env.storage.get_recent_decisions(limit=50)
