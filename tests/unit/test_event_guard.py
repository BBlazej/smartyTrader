"""Deterministic event guard (§7.18): calendar blackouts + delisting notices gate entries.

Exits are never gated; an unreadable context blocks entries (fail-closed); with
context off nothing changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.core.config import RiskSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import (
    OHLCV,
    Action,
    EventImportance,
    EventKind,
    MarketEvent,
    MarketSnapshot,
    RiskVerdict,
    SymbolContext,
    TradeSignal,
)
from src.core.risk_engine import RiskEngine
from src.execution.paper_executor import PaperExecutor

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def macro(at: datetime, importance: EventImportance = EventImportance.HIGH) -> MarketEvent:
    return MarketEvent(
        source="config",
        kind=EventKind.MACRO,
        at=at,
        title="FOMC rate decision",
        currency="USD",
        importance=importance,
    )


def earnings(at: datetime) -> MarketEvent:
    return MarketEvent(
        source="yfinance", kind=EventKind.EARNINGS, at=at, title="AAPL earnings", asset="AAPL"
    )


def delisting(at: datetime, asset: str = "BTC") -> MarketEvent:
    return MarketEvent(
        source="okx", kind=EventKind.DELISTING, at=at, title="OKX to delist", asset=asset
    )


def buy(symbol: str = "BTC/EUR") -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        action=Action.BUY,
        confidence=0.9,
        reasoning="setup",
        stop_loss=95.0,
        take_profit=110.0,
    )


def ctx(events=(), notices=(), now: datetime = NOW, symbol: str = "BTC/EUR") -> SymbolContext:
    return SymbolContext(symbol=symbol, now=now, events=list(events), notices=list(notices))


class TestBlackoutRules:
    engine = RiskEngine(RiskSettings())

    @pytest.mark.parametrize(
        ("offset", "blocked"),
        [
            (timedelta(minutes=121), False),  # event 2h01 away → free
            (timedelta(minutes=120), True),  # exactly 2h before → blocked
            (timedelta(0), True),
            (timedelta(minutes=-60), True),  # 1h after → still blocked
            (timedelta(minutes=-61), False),
        ],
    )
    def test_macro_window(self, offset: timedelta, blocked: bool) -> None:
        reason = self.engine.event_blackout_reason(ctx(events=[macro(NOW + offset)]))
        assert (reason is not None) is blocked
        if blocked:
            assert "USD FOMC rate decision" in reason
            assert "120 min before to 60 min after" in reason

    def test_importance_floor(self) -> None:
        medium = ctx(events=[macro(NOW, EventImportance.MEDIUM)])
        assert self.engine.event_blackout_reason(medium) is None
        loose = RiskEngine(RiskSettings(event_guard_min_importance="medium"))
        assert loose.event_blackout_reason(medium) is not None

    @pytest.mark.parametrize(
        ("offset", "blocked"),
        [
            (timedelta(days=1, minutes=1), False),
            (timedelta(hours=23), True),
            (timedelta(hours=-24), True),
            (timedelta(hours=-25), False),
        ],
    )
    def test_earnings_window(self, offset: timedelta, blocked: bool) -> None:
        context = ctx(events=[earnings(NOW + offset)], symbol="AAPL")
        assert (self.engine.event_blackout_reason(context) is not None) is blocked

    @pytest.mark.parametrize(
        ("age", "blocked"),
        [
            (timedelta(days=10), True),
            (timedelta(days=90), True),
            (timedelta(days=91), False),
            (timedelta(hours=-12), True),  # stamped slightly ahead of our clock
        ],
    )
    def test_delisting_window(self, age: timedelta, blocked: bool) -> None:
        context = ctx(notices=[delisting(NOW - age)])
        assert (self.engine.event_blackout_reason(context) is not None) is blocked

    def test_disabled_or_no_context(self) -> None:
        off = RiskEngine(RiskSettings(event_guard_enabled=False))
        assert off.event_blackout_reason(ctx(events=[macro(NOW)])) is None
        assert self.engine.event_blackout_reason(None) is None


class TestCheckEventGuard:
    engine = RiskEngine(RiskSettings())

    def test_blocks_buy(self) -> None:
        result = self.engine.check_event_guard(buy(), ctx(events=[macro(NOW)]))
        assert result.verdict is RiskVerdict.REJECTED
        assert result.reason.startswith("Event guard:")

    def test_never_gates_exits_or_holds(self) -> None:
        blocked = ctx(events=[macro(NOW)], notices=[delisting(NOW)])
        for action in (Action.SELL, Action.HOLD):
            signal = TradeSignal(symbol="BTC/EUR", action=action, confidence=0.9, reasoning="r")
            assert self.engine.check_event_guard(signal, blocked).verdict is RiskVerdict.APPROVED
            assert (
                self.engine.check_event_guard(signal, None, "db gone").verdict
                is RiskVerdict.APPROVED
            )

    def test_unreadable_context_blocks_entries(self) -> None:
        result = self.engine.check_event_guard(buy(), None, "db gone")
        assert result.verdict is RiskVerdict.REJECTED
        assert "market context unavailable (db gone)" in result.reason

    def test_clear_calendar_approves(self) -> None:
        result = self.engine.check_event_guard(buy(), ctx(events=[macro(NOW + timedelta(days=1))]))
        assert result.verdict is RiskVerdict.APPROVED


# ── Pipeline end to end ────────────────────────────────────────


def candles() -> list[OHLCV]:
    return [
        OHLCV(
            timestamp=NOW - timedelta(hours=40 - i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1000.0,
        )
        for i in range(40)
    ]


class FakeReader:
    def __init__(self, context: SymbolContext | None = None, error: Exception | None = None):
        self._context = context
        self._error = error
        self.calls = 0

    async def for_symbol(self, symbol: str, now: datetime) -> SymbolContext:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._context.model_copy(update={"symbol": symbol, "now": now})


def pipeline(signal: TradeSignal, reader: FakeReader | None) -> tuple[DecisionPipeline, AsyncMock]:
    provider = AsyncMock()
    provider.fetch_snapshot.return_value = MarketSnapshot(
        symbol=signal.symbol, timeframe="1h", candles=candles(), fetched_at=NOW
    )
    llm = AsyncMock()
    llm.ask_trade_signal.return_value = signal
    settings = RiskSettings(max_position_pct=0.5, max_stop_distance_pct=0.5)
    return (
        DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=RiskEngine(settings),
            executor=PaperExecutor(initial_cash=10_000.0),
            context_reader=reader,
        ),
        llm,
    )


class TestPipelineGuard:
    async def test_blackout_rejects_buy_and_is_announced_in_prompt(self) -> None:
        pipe, llm = pipeline(buy(), FakeReader(ctx(events=[macro(NOW + timedelta(minutes=30))])))
        result = await pipe.run("BTC/EUR", "1h")
        assert result.risk_result.verdict is RiskVerdict.REJECTED
        assert "Event guard" in result.risk_result.reason
        assert result.order_result is None
        prompt = llm.ask_trade_signal.call_args.kwargs["user_prompt"]
        assert "ENTRY BLACKOUT: Event guard: USD FOMC rate decision" in prompt

    async def test_clear_calendar_trades(self) -> None:
        pipe, llm = pipeline(buy(), FakeReader(ctx(events=[macro(NOW + timedelta(days=1))])))
        result = await pipe.run("BTC/EUR", "1h")
        assert result.executed is True
        prompt = llm.ask_trade_signal.call_args.kwargs["user_prompt"]
        assert "MARKET CONTEXT" in prompt and "ENTRY BLACKOUT" not in prompt

    async def test_unreadable_context_blocks_entry_but_still_asks(self) -> None:
        pipe, llm = pipeline(buy(), FakeReader(error=RuntimeError("db gone")))
        result = await pipe.run("BTC/EUR", "1h")
        assert result.risk_result.verdict is RiskVerdict.REJECTED
        assert "market context unavailable" in result.risk_result.reason
        assert "MARKET CONTEXT" not in llm.ask_trade_signal.call_args.kwargs["user_prompt"]

    async def test_sell_during_blackout_closes(self) -> None:
        sell = TradeSignal(symbol="BTC/EUR", action=Action.SELL, confidence=0.9, reasoning="exit")
        pipe, _ = pipeline(sell, FakeReader(ctx(notices=[delisting(NOW)])))
        await pipe.executor.place_order(symbol="BTC/EUR", side="buy", quantity=1.0, price=100.0)
        result = await pipe.run("BTC/EUR", "1h")
        assert result.executed is True
        assert result.order_result.side.value == "sell"

    async def test_context_off_is_unchanged(self) -> None:
        pipe, llm = pipeline(buy(), None)
        result = await pipe.run("BTC/EUR", "1h")
        assert result.executed is True
        assert "MARKET CONTEXT" not in llm.ask_trade_signal.call_args.kwargs["user_prompt"]
