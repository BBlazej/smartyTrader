"""Tests for the monitoring package — logging setup + alert dispatching."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.crypto_agent import CryptoAgent
from src.core.config import RiskSettings
from src.core.decision_pipeline import PipelineResult
from src.core.models import (
    OrderResult,
    OrderSide,
    PortfolioState,
    RiskResult,
    RiskVerdict,
    TradeSignal,
)
from src.core.risk_engine import RiskEngine
from src.monitoring import AlertManager, NoopAlertSink
from src.monitoring.logger import setup_logging

# ── Logging setup ─────────────────────────────────────────────


class TestSetupLogging:
    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_does_not_raise_for_levels(self, level: str) -> None:
        # Regression: structlog.configure() does not accept a ``level`` kwarg in
        # the installed version, so this must go through a filtering wrapper.
        setup_logging(level)

    def test_reconfigurable(self) -> None:
        setup_logging("WARNING")
        setup_logging("INFO")  # calling again must not raise


# ── Noop sink ─────────────────────────────────────────────────


class TestNoopAlertSink:
    @pytest.mark.asyncio
    async def test_send_returns_true(self) -> None:
        assert await NoopAlertSink().send("event", "msg", "info") is True


# ── AlertManager ──────────────────────────────────────────────


class RecordingSink:
    """Captures sent alerts for assertions."""

    def __init__(self, ok: bool = True, exc: Exception | None = None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.ok = ok
        self.exc = exc

    async def send(self, event: str, message: str, severity: str) -> bool:
        if self.exc:
            raise self.exc
        self.sent.append((event, message, severity))
        return self.ok


class TestAlertManager:
    def test_defaults_to_noop_sink(self) -> None:
        manager = AlertManager()
        assert isinstance(manager.sinks[0], NoopAlertSink)

    @pytest.mark.asyncio
    async def test_delivers_to_sink(self) -> None:
        sink = RecordingSink()
        manager = AlertManager(sinks=[sink])
        assert await manager.send("order_filled", "BTC", severity="info") is True
        assert sink.sent == [("order_filled", "BTC", "info")]

    @pytest.mark.asyncio
    async def test_dedup_suppresses_repeat_within_window(self) -> None:
        sink = RecordingSink()
        manager = AlertManager(sinks=[sink], dedup_window=300.0, clock=lambda: 100.0)
        await manager.send("risk_rejected", "low confidence", symbol="BTC/USDT")
        await manager.send("risk_rejected", "low confidence", symbol="BTC/USDT")
        # Only one delivery.
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_dedup_allows_after_window(self) -> None:
        sink = RecordingSink()
        clock = {"t": 100.0}
        manager = AlertManager(sinks=[sink], dedup_window=300.0, clock=lambda: clock["t"])
        await manager.send("order_filled", "a", symbol="BTC/USDT")
        clock["t"] = 100.0 + 301.0  # beyond the window
        await manager.send("order_filled", "b", symbol="BTC/USDT")
        assert len(sink.sent) == 2

    @pytest.mark.asyncio
    async def test_dedup_is_per_symbol(self) -> None:
        sink = RecordingSink()
        manager = AlertManager(sinks=[sink], dedup_window=300.0, clock=lambda: 100.0)
        await manager.send("order_filled", "x", symbol="BTC/USDT")
        await manager.send("order_filled", "x", symbol="ETH/USDT")
        assert len(sink.sent) == 2

    @pytest.mark.asyncio
    async def test_one_failing_sink_still_delivers(self) -> None:
        good = RecordingSink(ok=True)
        bad = RecordingSink(ok=False)
        manager = AlertManager(sinks=[bad, good])
        assert await manager.send("event", "msg") is True
        assert len(good.sent) == 1

    @pytest.mark.asyncio
    async def test_all_sinks_failing_returns_false(self) -> None:
        bad = RecordingSink(ok=False)
        manager = AlertManager(sinks=[bad])
        assert await manager.send("event", "msg") is False

    @pytest.mark.asyncio
    async def test_exceptional_sink_does_not_block_others(self) -> None:
        good = RecordingSink(ok=True)
        raiser = RecordingSink(exc=RuntimeError("boom"))
        manager = AlertManager(sinks=[raiser, good])
        assert await manager.send("event", "msg") is True
        assert len(good.sent) == 1

    @pytest.mark.asyncio
    async def test_reset_dedup(self) -> None:
        sink = RecordingSink()
        manager = AlertManager(sinks=[sink], dedup_window=300.0, clock=lambda: 100.0)
        await manager.send("event", "msg", symbol="BTC/USDT")
        manager.reset_dedup()
        await manager.send("event", "msg", symbol="BTC/USDT")
        assert len(sink.sent) == 2


# ── Agent alerting ────────────────────────────────────────────


def _risk_engine() -> RiskEngine:
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


def _agent(sink: RecordingSink) -> tuple[CryptoAgent, MagicMock]:
    pipeline = MagicMock()
    pipeline.get_portfolio_state = AsyncMock(
        return_value=PortfolioState(cash=10_000.0, positions=[])
    )
    # A mock storage sidesteps the real SQLite lifecycle in these alerting tests.
    storage = MagicMock()
    for method in (
        "save_llm_decision",
        "save_market_snapshot",
        "save_order",
        "save_portfolio_snapshot",
    ):
        setattr(storage, method, AsyncMock())
    agent = CryptoAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=_risk_engine(),
        llm_client=AsyncMock(),
        pairs=["BTC/USDT"],
        alerts=AlertManager(sinks=[sink]),
    )
    return agent, pipeline


class TestAgentAlerting:
    @pytest.mark.asyncio
    async def test_alerts_on_filled_order(self) -> None:
        sink = RecordingSink()
        agent, pipeline = _agent(sink)
        pipeline.run = AsyncMock(
            return_value=PipelineResult(
                symbol="BTC/USDT",
                signal=TradeSignal(
                    symbol="BTC/USDT", action="buy", confidence=0.85, reasoning="go", stop_loss=95.0
                ),
                risk_result=RiskResult(verdict=RiskVerdict.APPROVED),
                order_result=OrderResult(
                    order_id="o1",
                    symbol="BTC/USDT",
                    side=OrderSide.BUY,
                    quantity=1.0,
                    price=100.0,
                    status="filled",
                ),
            )
        )
        await agent.run_cycle()
        assert ("order_filled", "BUY 1.0 BTC/USDT @ 100.0", "info") in sink.sent

    @pytest.mark.asyncio
    async def test_alerts_on_risk_rejection(self) -> None:
        sink = RecordingSink()
        agent, pipeline = _agent(sink)
        pipeline.run = AsyncMock(
            return_value=PipelineResult(
                symbol="BTC/USDT",
                signal=TradeSignal(
                    symbol="BTC/USDT", action="buy", confidence=0.4, reasoning="weak"
                ),
                risk_result=RiskResult(verdict=RiskVerdict.REJECTED, reason="Low confidence"),
            )
        )
        await agent.run_cycle()
        assert ("risk_rejected", "Low confidence", "warning") in sink.sent

    @pytest.mark.asyncio
    async def test_no_alert_for_plain_hold(self) -> None:
        sink = RecordingSink()
        agent, pipeline = _agent(sink)
        pipeline.run = AsyncMock(
            return_value=PipelineResult(
                symbol="BTC/USDT",
                signal=TradeSignal(
                    symbol="BTC/USDT", action="hold", confidence=0.5, reasoning="flat"
                ),
                risk_result=RiskResult(verdict=RiskVerdict.APPROVED),
            )
        )
        await agent.run_cycle()
        assert sink.sent == []
