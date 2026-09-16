"""Unit tests for the Kraken testnet executor (via CCXT)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.models import OrderSide
from src.execution.kraken_executor import KrakenExecutor


@pytest.fixture()
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def executor(mock_client: AsyncMock) -> KrakenExecutor:
    return KrakenExecutor(mock_client, quote_currency="USDT")


class TestPlaceOrder:
    @pytest.mark.asyncio
    async def test_market_order(self, executor: KrakenExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {
            "id": "order-123",
            "status": "closed",
            "amount": 0.5,
        }

        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=0.5)

        mock_client.create_order.assert_awaited_once_with(
            "BTC/USDT", "market", "buy", 0.5, price=None
        )
        assert result.order_id == "order-123"
        assert result.status == "filled"
        assert result.side == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_limit_order(self, executor: KrakenExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {
            "id": "order-456",
            "status": "open",
            "amount": 1.0,
        }

        result = await executor.place_order("ETH/USDT", OrderSide.SELL, quantity=1.0, price=2000.0)

        mock_client.create_order.assert_awaited_once_with(
            "ETH/USDT", "limit", "sell", 1.0, price=2000.0
        )
        assert result.status == "pending"
        assert result.price == 2000.0

    @pytest.mark.asyncio
    async def test_rejected_order(self, executor: KrakenExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {"id": "order-789", "status": "rejected"}

        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)
        assert result.status == "rejected"

    @pytest.mark.asyncio
    async def test_empty_response(self, executor: KrakenExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = None
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)
        assert result.order_id == ""
        assert result.status == "pending"


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_returns_positions(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_positions.return_value = [
            {
                "symbol": "BTC/USDT",
                "side": "long",
                "contracts": 0.5,
                "entryPrice": 50000.0,
                "markPrice": 51000.0,
            },
            {
                "symbol": "ETH/USDT",
                "side": "short",
                "contracts": 2.0,
                "entryPrice": 2500.0,
                "markPrice": 2400.0,
            },
        ]

        positions = await executor.get_positions()

        assert len(positions) == 2
        assert positions[0].symbol == "BTC/USDT"
        assert positions[0].quantity == 0.5
        assert positions[0].avg_entry_price == 50000.0
        assert positions[0].current_price == 51000.0

    @pytest.mark.asyncio
    async def test_skips_zero_contracts(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_positions.return_value = [
            {"symbol": "BTC/USDT", "side": "long", "contracts": 0.0, "entryPrice": 50000.0},
            {"symbol": "ETH/USDT", "side": "long", "contracts": 1.0, "entryPrice": 2500.0},
        ]
        positions = await executor.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "ETH/USDT"

    @pytest.mark.asyncio
    async def test_empty_response(self, executor: KrakenExecutor, mock_client: AsyncMock) -> None:
        mock_client.fetch_positions.return_value = []
        assert await executor.get_positions() == []

    @pytest.mark.asyncio
    async def test_none_response(self, executor: KrakenExecutor, mock_client: AsyncMock) -> None:
        mock_client.fetch_positions.return_value = None
        assert await executor.get_positions() == []


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_known_order(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        # First place an order to register the symbol
        mock_client.create_order.return_value = {"id": "order-1", "status": "open"}
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)

        result = await executor.cancel_order("order-1")
        assert result is True
        mock_client.cancel_order.assert_awaited_once_with("order-1", "BTC/USDT")

    @pytest.mark.asyncio
    async def test_cancel_unknown_order(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        result = await executor.cancel_order("nonexistent")
        assert result is False
        mock_client.cancel_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_failure_returns_false(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"id": "order-1", "status": "open"}
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)

        mock_client.cancel_order.side_effect = Exception("order already filled")
        result = await executor.cancel_order("order-1")
        assert result is False


class TestGetCash:
    @pytest.mark.asyncio
    async def test_returns_balance(self, executor: KrakenExecutor, mock_client: AsyncMock) -> None:
        mock_client.fetch_free_balance.return_value = 50000.0
        cash = await executor.get_cash()
        assert cash == 50000.0
        mock_client.fetch_free_balance.assert_awaited_once_with("USDT")


class TestRealCcxtShapes:
    """Payload shapes taken from real ccxt (4.5.x) responses [§7.6].

    The keyed path used to crash or silently mis-report on every one of these:
    ``float(balance)`` on a dict, no fill metadata on closed orders, and an
    exception every cycle because Kraken spot rejects ``fetch_positions``.
    """

    @pytest.mark.asyncio
    async def test_nested_free_balance_dict(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        # Real fetch_free_balance(): currency code → {free, used, total}.
        mock_client.fetch_free_balance.return_value = {
            "BTC": {"free": 0.5, "used": 0.0, "total": 0.5},
            "USDT": {"free": 1234.5, "used": 10.0, "total": 1244.5},
        }
        assert await executor.get_cash() == pytest.approx(1234.5)  # free, not total

    @pytest.mark.asyncio
    async def test_free_balance_total_fallback_and_case_insensitive_key(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_free_balance.return_value = {"usdt": {"total": 77.0}}
        assert await executor.get_cash() == pytest.approx(77.0)

    @pytest.mark.asyncio
    async def test_missing_quote_currency_is_zero(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_free_balance.return_value = {"BTC": {"free": 1.0}}
        assert await executor.get_cash() == 0.0

    @pytest.mark.asyncio
    async def test_closed_order_records_fill_price_and_time(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        # A marketable limit comes back closed in the create_order payload.
        mock_client.create_order.return_value = {
            "id": "D-XYZ",
            "status": "closed",
            "amount": 0.25,
            "filled": 0.25,
            "average": 61234.5,
            "timestamp": 1757900000000,
            "updated": 1757900005000,
        }

        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=0.25, price=61230.0)

        assert result.status == "filled"
        assert result.price == pytest.approx(61234.5)  # fill average, not the limit
        assert result.quantity == pytest.approx(0.25)
        assert result.filled_at is not None
        assert result.filled_at.timestamp() == pytest.approx(1757900005.0)

    @pytest.mark.asyncio
    async def test_open_order_stays_pending_without_fill_time(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"id": "D-OPEN", "status": "open"}
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=1.0)
        assert result.status == "pending"
        assert result.filled_at is None

    @pytest.mark.asyncio
    async def test_fetch_positions_not_supported_degrades_gracefully(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        # Kraken spot via CCXT raises NotSupported here; every cycle must not crash.
        class NotSupported(Exception):
            pass

        mock_client.fetch_positions.side_effect = NotSupported(
            "fetch_positions is not supported by Kraken"
        )
        assert await executor.get_positions() == []
        assert await executor.get_positions() == []  # repeat cycles stay safe


class TestRealizedPnlAttribution:
    """The keyed path tracks its own fills in a local FIFO ledger, so closing
    sells report realized PnL attributed back to the entry decision (§7.8).
    Venue commission is absent from create_order payloads, so it is gross."""

    @pytest.mark.asyncio
    async def test_closing_sell_realizes_pnl_and_attributes_entry(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {
            "id": "D-BUY",
            "status": "closed",
            "filled": 1.0,
            "average": 100.0,
        }
        await executor.place_order(
            "BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0, decision_id=9
        )

        mock_client.create_order.return_value = {
            "id": "D-SELL",
            "status": "closed",
            "filled": 1.0,
            "average": 120.0,
        }
        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=120.0)

        assert result.realized_pnl == pytest.approx(20.0)
        assert len(result.closed_entries) == 1
        assert result.closed_entries[0].entry_decision_id == 9
        assert result.closed_entries[0].pnl == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_untracked_holdings_report_no_outcome(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        # A sell of lots we never filled locally (e.g. opened before a restart)
        # must not fabricate a break-even outcome.
        mock_client.create_order.return_value = {
            "id": "D-SELL",
            "status": "closed",
            "filled": 1.0,
            "average": 120.0,
        }
        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=120.0)

        assert result.status == "filled"
        assert result.realized_pnl is None
        assert result.closed_entries == []

    @pytest.mark.asyncio
    async def test_pending_orders_are_not_tracked(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        # An unfilled buy must not create a lot a later sell could "close".
        mock_client.create_order.return_value = {"id": "D-OPEN", "status": "open"}
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        mock_client.create_order.return_value = {
            "id": "D-SELL",
            "status": "closed",
            "filled": 1.0,
            "average": 120.0,
        }
        result = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=120.0)
        assert result.realized_pnl is None


class TestExitLevelCarrying:
    """§7.9: exit levels from entry signals are re-attached to reported positions
    (ccxt payloads don't carry them) and dropped once the position closes."""

    @pytest.mark.asyncio
    async def test_levels_attached_then_dropped_on_close(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_positions.return_value = [
            {"symbol": "BTC/USDT", "contracts": 1.0, "entryPrice": 100.0}
        ]
        mock_client.create_order.return_value = {
            "id": "D-BUY",
            "status": "closed",
            "filled": 1.0,
            "average": 100.0,
        }
        await executor.place_order(
            "BTC/USDT",
            OrderSide.BUY,
            quantity=1.0,
            price=100.0,
            stop_loss=95.0,
            take_profit=120.0,
        )

        positions = await executor.get_positions()
        assert positions[0].stop_loss == 95.0
        assert positions[0].take_profit == 120.0

        mock_client.create_order.return_value = {
            "id": "D-SELL",
            "status": "closed",
            "filled": 1.0,
            "average": 90.0,
        }
        await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=90.0)

        assert executor._exit_levels == {}
        assert (await executor.get_positions())[0].stop_loss is None


class TestClose:
    """close() must release the keyed exchange's aiohttp session.

    In testnet mode the executor owns a dedicated CCXT exchange client; without
    an explicit close it leaks on shutdown.
    """

    @pytest.mark.asyncio
    async def test_closes_exchange_client(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        await executor.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(
        self, executor: KrakenExecutor, mock_client: AsyncMock
    ) -> None:
        await executor.close()
        await executor.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_without_close_method_is_noop(self) -> None:
        executor = KrakenExecutor(AsyncMock(spec=["create_order"]), quote_currency="USDT")
        await executor.close()  # must not raise
