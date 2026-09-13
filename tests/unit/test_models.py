"""Tests for shared data models."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.core.models import (
    OHLCV,
    Action,
    MarketSnapshot,
    OrderResult,
    OrderSide,
    PortfolioState,
    Position,
    RiskResult,
    RiskVerdict,
    TradeSignal,
)


class TestTradeSignal:
    def test_create_buy_signal(self) -> None:
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.85,
            reasoning="Strong bullish divergence on RSI",
            stop_loss=60000.0,
            take_profit=70000.0,
        )

        assert signal.action == Action.BUY
        assert signal.confidence == 0.85
        assert signal.stop_loss == 60000.0

    def test_hold_signal_no_stop_loss(self) -> None:
        signal = TradeSignal(
            symbol="ETH/USDT",
            action=Action.HOLD,
            confidence=0.4,
            reasoning="Unclear direction",
        )

        assert signal.stop_loss is None

    def test_confidence_bounds(self) -> None:
        # Valid bounds
        TradeSignal(symbol="X", action="hold", confidence=0.0, reasoning="")
        TradeSignal(symbol="X", action="hold", confidence=1.0, reasoning="")

        # Out of bounds should raise
        with pytest.raises(ValidationError):
            TradeSignal(symbol="X", action="hold", confidence=1.5, reasoning="")

    def test_serializable_to_json(self) -> None:
        signal = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.78,
            reasoning="Test",
        )

        data = json.loads(signal.model_dump_json())
        assert data["symbol"] == "BTC/USDT"
        assert data["action"] == "buy"


class TestPortfolioState:
    def test_total_value_with_positions(self) -> None:
        portfolio = PortfolioState(
            cash=10000.0,
            positions=[
                Position(symbol="AAPL", quantity=10, avg_entry_price=150.0, current_price=160.0),
            ],
        )

        assert portfolio.total_value == 10000.0 + 10 * 160.0

    def test_unrealized_pnl_positive(self) -> None:
        portfolio = PortfolioState(
            cash=5000.0,
            positions=[
                Position(symbol="MSFT", quantity=5, avg_entry_price=300.0, current_price=320.0),
            ],
        )

        assert portfolio.unrealized_pnl == 5 * (320.0 - 300.0)

    def test_unrealized_pnl_negative(self) -> None:
        portfolio = PortfolioState(
            cash=5000.0,
            positions=[
                Position(symbol="TSLA", quantity=2, avg_entry_price=250.0, current_price=230.0),
            ],
        )

        assert portfolio.unrealized_pnl == 2 * (230.0 - 250.0)

    def test_empty_portfolio(self) -> None:
        portfolio = PortfolioState(cash=10000.0, positions=[])

        assert portfolio.total_value == 10000.0
        assert portfolio.unrealized_pnl == 0.0


class TestPosition:
    def test_pnl_pct(self) -> None:
        pos = Position(symbol="X", quantity=1, avg_entry_price=100.0, current_price=120.0)

        assert pos.pnl_pct == pytest.approx(0.20)

    def test_pnl_pct_zero_entry(self) -> None:
        pos = Position(symbol="X", quantity=1, avg_entry_price=0.0, current_price=100.0)

        assert pos.pnl_pct == 0.0


class TestRiskResult:
    def test_approved_no_reason(self) -> None:
        result = RiskResult(verdict=RiskVerdict.APPROVED)

        assert result.verdict == RiskVerdict.APPROVED
        assert result.reason is None

    def test_rejected_with_reason(self) -> None:
        result = RiskResult(
            verdict=RiskVerdict.REJECTED,
            reason="Daily loss limit exceeded",
        )

        assert result.verdict == RiskVerdict.REJECTED
        assert "limit" in result.reason.lower()


class TestMarketSnapshot:
    def test_with_candles(self) -> None:
        snapshot = MarketSnapshot(
            symbol="BTC/USDT",
            timeframe="1h",
            candles=[
                OHLCV(timestamp=None, open=60000, high=61000, low=59500, close=60500, volume=100),  # type: ignore[arg-type]
            ],
        )

        assert len(snapshot.candles) == 1
        assert snapshot.indicators == {}


class TestOrderResult:
    def test_market_order(self) -> None:
        result = OrderResult(
            order_id="abc-123",
            symbol="ETH/USDT",
            side=OrderSide.BUY,
            quantity=0.5,
            status="filled",
        )

        assert result.price is None
        assert result.status == "filled"
