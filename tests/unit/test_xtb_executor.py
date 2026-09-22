"""Unit tests for the XTB (demo) executor (via xAPI)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.models import OrderSide
from src.execution.xtb_executor import XTBExecutor


@pytest.fixture()
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def executor(mock_client: AsyncMock) -> XTBExecutor:
    return XTBExecutor(mock_client)


class TestPlaceOrder:
    @pytest.mark.asyncio
    async def test_market_order(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {
            "order_id": "xtb-123",
            "status": "filled",
            "quantity": 10.0,
        }

        result = await executor.place_order("AAPL", OrderSide.BUY, quantity=10.0)

        mock_client.create_order.assert_awaited_once_with("AAPL", "buy", 10.0, price=None)
        assert result.order_id == "xtb-123"
        assert result.status == "filled"
        assert result.side == OrderSide.BUY
        assert result.quantity == 10.0

    @pytest.mark.asyncio
    async def test_limit_order(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {
            "order_id": "xtb-456",
            "status": "open",
            "quantity": 5.0,
        }

        result = await executor.place_order("MSFT", OrderSide.SELL, quantity=5.0, price=300.0)

        mock_client.create_order.assert_awaited_once_with("MSFT", "sell", 5.0, price=300.0)
        assert result.status == "pending"
        assert result.price == 300.0

    @pytest.mark.asyncio
    async def test_rejected_order(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {"order_id": "xtb-789", "status": "rejected"}

        result = await executor.place_order("AAPL", OrderSide.BUY, quantity=1.0)
        assert result.status == "rejected"

    @pytest.mark.asyncio
    async def test_empty_response(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = None
        result = await executor.place_order("AAPL", OrderSide.BUY, quantity=1.0)
        assert result.order_id == ""
        assert result.status == "pending"
        assert result.quantity == 1.0


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_returns_positions(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.get_positions.return_value = [
            {"symbol": "AAPL", "quantity": 10.0, "avg_entry_price": 150.0, "current_price": 155.0},
            {"symbol": "MSFT", "quantity": 5.0, "avg_entry_price": 300.0, "current_price": 290.0},
        ]

        positions = await executor.get_positions()

        assert len(positions) == 2
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == 10.0
        assert positions[0].avg_entry_price == 150.0
        assert positions[0].current_price == 155.0

    @pytest.mark.asyncio
    async def test_short_payloads_map_to_short_positions(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        """§7.38: xAPI side hint (and signed quantities) never fake a long."""
        from src.core.models import PositionSide

        mock_client.get_positions.return_value = [
            {"symbol": "TSLA", "quantity": 4.0, "side": "short", "avg_entry_price": 200.0,
             "current_price": 190.0},
        ]
        positions = await executor.get_positions()
        assert positions[0].side == PositionSide.SHORT
        assert positions[0].quantity == 4.0
        assert positions[0].pnl == pytest.approx(40.0)  # short gains on the fall

    @pytest.mark.asyncio
    async def test_skips_zero_quantity(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.get_positions.return_value = [
            {"symbol": "AAPL", "quantity": 0.0, "avg_entry_price": 150.0},
            {"symbol": "MSFT", "quantity": 5.0, "avg_entry_price": 300.0},
        ]
        positions = await executor.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "MSFT"

    @pytest.mark.asyncio
    async def test_empty_response(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.get_positions.return_value = []
        assert await executor.get_positions() == []

    @pytest.mark.asyncio
    async def test_none_response(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.get_positions.return_value = None
        assert await executor.get_positions() == []


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_success(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.cancel_order.return_value = {"order_id": "xtb-1"}
        result = await executor.cancel_order("xtb-1")
        assert result is True
        mock_client.cancel_order.assert_awaited_once_with("xtb-1")

    @pytest.mark.asyncio
    async def test_cancel_failure_returns_false(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.cancel_order.side_effect = Exception("order already filled")
        result = await executor.cancel_order("xtb-1")
        assert result is False


class TestGetCash:
    @pytest.mark.asyncio
    async def test_returns_balance(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        mock_client.get_balance.return_value = 25000.0
        cash = await executor.get_cash()
        assert cash == 25000.0
        mock_client.get_balance.assert_awaited_once()


class TestRealizedPnlAttribution:
    """Filled priced orders feed the executor's local FIFO ledger so closing
    sells realize PnL back to their entry decision (§7.8). xAPI reports no
    commission in create_order, so realized_pnl is gross."""

    @pytest.mark.asyncio
    async def test_closing_sell_realizes_pnl_and_attributes_entry(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {
            "order_id": "1",
            "status": "filled",
            "quantity": 2.0,
        }
        await executor.place_order("AAPL", OrderSide.BUY, quantity=2.0, price=100.0, decision_id=7)

        mock_client.create_order.return_value = {
            "order_id": "2",
            "status": "filled",
            "quantity": 2.0,
        }
        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=2.0, price=110.0)

        assert result.realized_pnl == pytest.approx(20.0)
        assert len(result.closed_entries) == 1
        assert result.closed_entries[0].entry_decision_id == 7
        assert result.closed_entries[0].pnl == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_untracked_holdings_report_no_outcome(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {
            "order_id": "1",
            "status": "filled",
            "quantity": 1.0,
        }
        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=1.0, price=50.0)

        assert result.status == "filled"
        assert result.realized_pnl is None
        assert result.closed_entries == []

    @pytest.mark.asyncio
    async def test_market_orders_without_price_are_not_tracked(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        # No fill price in the payload → nothing to base cost basis on.
        mock_client.create_order.return_value = {
            "order_id": "1",
            "status": "filled",
            "quantity": 1.0,
        }
        await executor.place_order("AAPL", OrderSide.BUY, quantity=1.0)

        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=1.0, price=60.0)
        assert result.realized_pnl is None


class TestExitLevelCarrying:
    """§7.9: exit levels from entry signals are re-attached to positions xAPI
    reports (the payloads don't carry them) and dropped once the position closes."""

    @pytest.mark.asyncio
    async def test_levels_attached_then_dropped_on_close(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.get_positions.return_value = [
            {"symbol": "AAPL", "quantity": 2.0, "avg_entry_price": 100.0}
        ]
        mock_client.create_order.return_value = {
            "order_id": "1",
            "status": "filled",
            "quantity": 2.0,
        }
        await executor.place_order(
            "AAPL",
            OrderSide.BUY,
            quantity=2.0,
            price=100.0,
            stop_loss=95.0,
            take_profit=120.0,
        )

        positions = await executor.get_positions()
        assert positions[0].stop_loss == 95.0
        assert positions[0].take_profit == 120.0

        mock_client.create_order.return_value = {
            "order_id": "2",
            "status": "filled",
            "quantity": 2.0,
        }
        await executor.place_order("AAPL", OrderSide.SELL, quantity=2.0, price=90.0)

        assert executor._exit_levels == {}
        assert (await executor.get_positions())[0].stop_loss is None


class TestClose:
    """close() must release the underlying xAPI client (its session, if any)."""

    @pytest.mark.asyncio
    async def test_closes_client(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        await executor.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, executor: XTBExecutor, mock_client: AsyncMock) -> None:
        await executor.close()
        await executor.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_without_close_method_is_noop(self) -> None:
        executor = XTBExecutor(AsyncMock(spec=["create_order"]))
        await executor.close()  # must not raise
