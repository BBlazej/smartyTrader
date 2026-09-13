"""Tests for the Paper (simulated) executor — the safe default for all testing."""

from __future__ import annotations

import pytest

from src.core.models import Executor, OrderSide
from src.execution.paper_executor import PaperExecutor


@pytest.fixture()
def executor() -> PaperExecutor:
    """Paper executor with slippage disabled so monetary assertions are exact."""
    return PaperExecutor(initial_cash=100_000.0, slippage_pct=0.0)


class TestBuy:
    async def test_buy_fills_and_reduces_cash(self, executor: PaperExecutor) -> None:
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        assert result.status == "filled"
        assert result.side == OrderSide.BUY
        assert result.price == 100.0
        assert result.realized_pnl is None  # A buy does not realize PnL
        assert executor.cash == 100_000.0 - 100.0

    async def test_buy_creates_position(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=2.0, price=50.0)

        positions = await executor.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "BTC/USDT"
        assert positions[0].quantity == 2.0
        assert positions[0].avg_entry_price == 50.0

    async def test_buy_without_price_and_no_position_rejected(
        self, executor: PaperExecutor
    ) -> None:
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)

        assert result.status == "rejected"
        assert "Cannot fill" in (result.reason or "")
        assert executor.cash == 100_000.0

    async def test_buy_insufficient_cash_rejected(self, executor: PaperExecutor) -> None:
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=2000.0, price=100.0)

        assert result.status == "rejected"
        assert "Insufficient cash" in (result.reason or "")
        # Rejected buy must not touch the balance
        assert executor.cash == 100_000.0

    async def test_buy_adds_to_existing_position_with_avg_cost(
        self, executor: PaperExecutor
    ) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=200.0)

        positions = await executor.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == 2.0
        assert positions[0].avg_entry_price == 150.0  # (100 + 200) / 2

    async def test_buy_applies_slippage(self) -> None:
        ex = PaperExecutor(initial_cash=100_000.0, slippage_pct=0.001)
        result = await ex.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        assert result.status == "filled"
        # Slippage pushes the fill price up for buys
        assert result.price == pytest.approx(100.1)
        assert ex.cash == pytest.approx(100_000.0 - 100.1)


class TestSell:
    async def test_sell_realizes_positive_pnl(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)
        before = executor.cash

        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=120.0)

        assert result.status == "filled"
        assert result.realized_pnl == pytest.approx(20.0)  # (120 - 100) * 1
        assert executor.cash == pytest.approx(before + 120.0)

    async def test_sell_realizes_negative_pnl(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=80.0)

        assert result.status == "filled"
        assert result.realized_pnl == pytest.approx(-20.0)  # (80 - 100) * 1

    async def test_partial_sell_keeps_position(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=2.0, price=100.0)

        await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=110.0)

        positions = await executor.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == 1.0

    async def test_full_sell_closes_position(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=110.0)

        assert result.status == "filled"
        assert await executor.get_positions() == []

    async def test_sell_without_position_rejected(self, executor: PaperExecutor) -> None:
        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=100.0)

        assert result.status == "rejected"
        assert "Insufficient position" in (result.reason or "")

    async def test_sell_more_than_held_rejected(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=2.0, price=100.0)

        assert result.status == "rejected"
        assert "Insufficient position" in (result.reason or "")
        # Rejected sell leaves the position untouched
        assert (await executor.get_positions())[0].quantity == 1.0

    async def test_sell_applies_slippage(self) -> None:
        ex = PaperExecutor(initial_cash=100_000.0, slippage_pct=0.001)
        await ex.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        result = await ex.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=110.0)

        assert result.status == "filled"
        # Slippage pushes the fill price down for sells
        assert result.price == pytest.approx(109.89)


class TestFees:
    """Fee modeling: per-side commission deducted from cash and realized PnL.

    Uses a generous 1% fee so the fee component is large enough to assert
    exactly. The default ``fee_pct=0.0`` keeps the fee-free tests above valid.
    """

    @pytest.fixture()
    def fee_executor(self) -> PaperExecutor:
        return PaperExecutor(initial_cash=100_000.0, slippage_pct=0.0, fee_pct=0.01)

    async def test_buy_deducts_fee(self, fee_executor: PaperExecutor) -> None:
        result = await fee_executor.place_order(
            "BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0
        )

        assert result.status == "filled"
        # Cash drops by cost + fee (100 + 1.0), not just the notional.
        assert fee_executor.cash == 100_000.0 - 101.0

    async def test_buy_insufficient_cash_with_fee_rejected(
        self, fee_executor: PaperExecutor
    ) -> None:
        # Cash exactly covers the notional but has no room for the fee → rejected.
        fee_executor._cash = 100.0

        result = await fee_executor.place_order(
            "BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0
        )

        assert result.status == "rejected"
        assert "Insufficient cash" in (result.reason or "")
        # Rejected buy leaves the balance untouched.
        assert fee_executor.cash == 100.0

    async def test_sell_realized_pnl_is_net_of_fees(self, fee_executor: PaperExecutor) -> None:
        await fee_executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)
        before = fee_executor.cash

        result = await fee_executor.place_order(
            "BTC/USDT", OrderSide.SELL, quantity=1.0, price=120.0
        )

        assert result.status == "filled"
        # Net PnL = gross(20) - buy_fee(1.0) - sell_fee(1.2) = 17.8
        assert result.realized_pnl == pytest.approx(17.8)
        # Sell credits proceeds net of the sell fee: 120 - 1.2 = 118.8
        assert fee_executor.cash == pytest.approx(before + 118.8)

    async def test_default_fee_is_zero(self, executor: PaperExecutor) -> None:
        # The shared ``executor`` fixture (fee_pct=0.0) must behave fee-free.
        assert executor.fee_pct == 0.0
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)
        assert executor.cash == 100_000.0 - 100.0


class TestGetPositions:
    async def test_empty_initially(self, executor: PaperExecutor) -> None:
        assert await executor.get_positions() == []

    async def test_returns_all_positions(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)
        await executor.place_order("ETH/USDT", OrderSide.BUY, quantity=1.0, price=50.0)

        positions = await executor.get_positions()
        assert {p.symbol for p in positions} == {"BTC/USDT", "ETH/USDT"}


class TestCancelOrder:
    async def test_cancel_nonexistent_returns_false(self, executor: PaperExecutor) -> None:
        assert await executor.cancel_order("does-not-exist") is False

    async def test_cancel_filled_order_returns_false(self, executor: PaperExecutor) -> None:
        # Paper orders fill instantly, so there is no pending order to cancel.
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        assert result.status == "filled"
        assert await executor.cancel_order(result.order_id) is False


class TestProtocolCompliance:
    """PaperExecutor is the default executor — it must satisfy the Executor contract."""

    def test_isinstance_executor_protocol(self, executor: PaperExecutor) -> None:
        assert isinstance(executor, Executor)

    async def test_get_cash_matches_balance(self, executor: PaperExecutor) -> None:
        assert await executor.get_cash() == 100_000.0

        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)
        assert await executor.get_cash() == 100_000.0 - 100.0

    async def test_all_protocol_members_present(self, executor: PaperExecutor) -> None:
        # Guards against a future executor forgetting a member of the contract.
        for member in ("place_order", "get_positions", "cancel_order", "get_cash", "close"):
            assert hasattr(executor, member)

    async def test_close_is_noop(self, executor: PaperExecutor) -> None:
        # No persistent connections → close is a no-op that returns cleanly.
        assert await executor.close() is None


class TestUpdatePrice:
    async def test_updates_existing_position(self, executor: PaperExecutor) -> None:
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        executor.update_price("BTC/USDT", 120.0)

        positions = await executor.get_positions()
        assert positions[0].current_price == 120.0

    async def test_noop_for_unknown_symbol(self, executor: PaperExecutor) -> None:
        # Must not raise — used defensively when a symbol is delisted or sold.
        executor.update_price("BTC/USDT", 120.0)

        assert await executor.get_positions() == []
