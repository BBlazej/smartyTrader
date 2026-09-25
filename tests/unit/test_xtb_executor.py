"""Unit tests for the XTB (demo) executor (via xAPI)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.models import OrderSide
from src.execution.xtb_executor import XTBExecutor


@pytest.fixture()
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_open_trades.return_value = []  # flat venue book unless a test says otherwise
    return client


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

        result = await executor.place_order("MSFT", OrderSide.BUY, quantity=5.0, price=300.0)

        mock_client.create_order.assert_awaited_once_with("MSFT", "buy", 5.0, price=300.0)
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
            {
                "symbol": "TSLA",
                "quantity": 4.0,
                "side": "short",
                "avg_entry_price": 200.0,
                "current_price": 190.0,
            },
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

        # The SELL closes the open trade (§7.40) — it is never an OPEN transaction.
        mock_client.get_open_trades.return_value = [
            {"order": 11, "symbol": "AAPL", "cmd": 0, "volume": 2.0, "open_price": 100.0}
        ]
        mock_client.close_trade.return_value = {
            "order_id": "12",
            "status": "filled",
            "quantity": 2.0,
            "price": 110.0,
        }
        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=2.0, price=110.0)

        mock_client.close_trade.assert_awaited_once_with(11, "AAPL", 0, 2.0, price=110.0)
        assert mock_client.create_order.await_count == 1  # only the entry
        assert result.realized_pnl == pytest.approx(20.0)
        assert len(result.closed_entries) == 1
        assert result.closed_entries[0].entry_decision_id == 7
        assert result.closed_entries[0].pnl == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_untracked_holdings_report_no_outcome(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        # Holdings opened before a restart: open at the venue, absent from the ledger.
        mock_client.get_open_trades.return_value = [
            {"order": 3, "symbol": "AAPL", "cmd": 0, "volume": 1.0, "open_price": 40.0}
        ]
        mock_client.close_trade.return_value = {
            "order_id": "4",
            "status": "filled",
            "quantity": 1.0,
            "price": 50.0,
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

        mock_client.get_open_trades.return_value = [
            {"order": 5, "symbol": "AAPL", "cmd": 0, "volume": 1.0, "open_price": 55.0}
        ]
        mock_client.close_trade.return_value = {"order_id": "6", "status": "filled", "price": 60.0}
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

        mock_client.get_open_trades.return_value = [
            {"order": 1, "symbol": "AAPL", "cmd": 0, "volume": 2.0, "open_price": 100.0}
        ]
        mock_client.close_trade.return_value = {"order_id": "2", "status": "filled", "price": 90.0}
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


class TestReduceFirstNeverFlip:
    """§7.40: xAPI SELL/OPEN opens a short — reducing orders must CLOSE trades."""

    @staticmethod
    def _trade(order: int, cmd: int, volume: float) -> dict:
        return {"order": order, "symbol": "AAPL", "cmd": cmd, "volume": volume, "open_price": 100.0}

    async def test_sell_with_no_open_long_is_refused(self, executor, mock_client) -> None:
        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=1.0, price=100.0)
        assert result.status == "rejected" and "would open a short" in (result.reason or "")
        mock_client.create_order.assert_not_awaited()
        mock_client.close_trade.assert_not_awaited()

    async def test_short_opening_can_be_explicitly_allowed(self, mock_client) -> None:
        mock_client.create_order.return_value = {"order_id": "9", "status": "filled"}
        permissive = XTBExecutor(mock_client, allow_short=True)
        await permissive.place_order("AAPL", OrderSide.SELL, quantity=1.0, price=100.0)
        mock_client.create_order.assert_awaited_once_with("AAPL", "sell", 1.0, price=100.0)

    async def test_buy_covers_an_open_short(self, executor, mock_client) -> None:
        mock_client.get_open_trades.return_value = [self._trade(21, 1, 3.0)]
        mock_client.close_trade.return_value = {"order_id": "22", "status": "filled", "price": 95.0}
        result = await executor.place_order("AAPL", OrderSide.BUY, quantity=3.0, price=95.0)
        mock_client.close_trade.assert_awaited_once_with(21, "AAPL", 1, 3.0, price=95.0)
        mock_client.create_order.assert_not_awaited()
        assert result.status == "filled"

    async def test_partial_close_across_trades_fifo(self, executor, mock_client) -> None:
        mock_client.get_open_trades.return_value = [self._trade(1, 0, 1.0), self._trade(2, 0, 2.0)]
        mock_client.close_trade.return_value = {"order_id": "x", "status": "filled", "price": 101.0}
        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=2.5, price=101.0)
        calls = [c.args for c in mock_client.close_trade.await_args_list]
        assert calls == [(1, "AAPL", 0, 1.0), (2, "AAPL", 0, 1.5)]
        assert result.quantity == pytest.approx(2.5)

    async def test_oversized_close_never_flips(self, executor, mock_client) -> None:
        mock_client.get_open_trades.return_value = [self._trade(1, 0, 1.0)]
        mock_client.close_trade.return_value = {"order_id": "x", "status": "filled", "price": 99.0}
        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=3.0, price=99.0)
        assert result.quantity == pytest.approx(1.0)
        mock_client.create_order.assert_not_awaited()  # the 2-lot remainder is NOT a short

    async def test_failed_close_is_reported(self, executor, mock_client) -> None:
        mock_client.get_open_trades.return_value = [self._trade(1, 0, 1.0)]
        mock_client.close_trade.return_value = {"order_id": "", "status": "rejected"}
        result = await executor.place_order("AAPL", OrderSide.SELL, quantity=1.0, price=99.0)
        assert result.status == "rejected" and "came back rejected" in (result.reason or "")

    async def test_other_symbols_trades_are_ignored(self, executor, mock_client) -> None:
        mock_client.get_open_trades.return_value = []  # the client filters by symbol
        await executor.place_order("MSFT", OrderSide.SELL, quantity=1.0, price=1.0)
        mock_client.get_open_trades.assert_awaited_with("MSFT")


class TestSymbolMap:
    """§7.59 L8: data symbols (yfinance ``AAPL``) ↔ xAPI symbols (``AAPL.US``)."""

    @pytest.fixture()
    def mapped(self, mock_client: AsyncMock) -> XTBExecutor:
        return XTBExecutor(mock_client, symbol_map={"AAPL": "AAPL.US"})

    async def test_orders_and_closes_use_venue_symbols(
        self, mapped: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"order_id": "1", "status": "filled"}
        await mapped.place_order("AAPL", OrderSide.BUY, 2.0, price=100.0, decision_id=4)
        mock_client.get_open_trades.assert_awaited_with("AAPL.US")
        assert mock_client.create_order.await_args.args[0] == "AAPL.US"

        mock_client.get_open_trades.return_value = [
            {"order": 11, "symbol": "AAPL.US", "cmd": 0, "volume": 2.0}
        ]
        mock_client.close_trade.return_value = {"order_id": "12", "status": "filled"}
        sell = await mapped.place_order("AAPL", OrderSide.SELL, 2.0, price=110.0)
        mock_client.close_trade.assert_awaited_once_with(11, "AAPL.US", 0, 2.0, price=110.0)
        # Ledger/attribution stay in data-symbol space.
        assert sell.symbol == "AAPL"
        assert sell.closed_entries[0].entry_decision_id == 4

    async def test_positions_map_back_with_levels(
        self, mapped: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"order_id": "1", "status": "filled"}
        await mapped.place_order("AAPL", OrderSide.BUY, 1.0, price=100.0, stop_loss=95.0)
        mock_client.get_positions.return_value = [
            {"symbol": "AAPL.US", "quantity": 1.0, "avg_entry_price": 100.0},
            {"symbol": "MSFT.US", "quantity": 1.0, "avg_entry_price": 300.0},  # unmapped
        ]
        positions = {p.symbol: p for p in await mapped.get_positions()}
        assert set(positions) == {"AAPL", "MSFT.US"}
        assert positions["AAPL"].stop_loss == 95.0

    async def test_unmapped_symbols_pass_through(
        self, executor: XTBExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"order_id": "1", "status": "filled"}
        await executor.place_order("AAPL", OrderSide.BUY, 1.0, price=100.0)
        assert mock_client.create_order.await_args.args[0] == "AAPL"
