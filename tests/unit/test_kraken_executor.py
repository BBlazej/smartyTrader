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
