"""Unit tests for the keyed Kraken executor (via CCXT)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.models import OrderSide
from src.execution.ccxt_executor import CcxtExecutor


@pytest.fixture()
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def executor(mock_client: AsyncMock) -> CcxtExecutor:
    return CcxtExecutor(mock_client, quote_currency="USDT", venue="test")


class TestPlaceOrder:
    @pytest.mark.asyncio
    async def test_market_order(self, executor: CcxtExecutor, mock_client: AsyncMock) -> None:
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
    async def test_limit_order(self, executor: CcxtExecutor, mock_client: AsyncMock) -> None:
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
    async def test_rejected_order(self, executor: CcxtExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {"id": "order-789", "status": "rejected"}

        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)
        assert result.status == "rejected"

    @pytest.mark.asyncio
    async def test_empty_response(self, executor: CcxtExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = None
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)
        assert result.order_id == ""
        assert result.status == "pending"


class TestSpotOnlyPositions:
    """§7.64: positions always come from the fill ledger — ``fetch_positions`` is never
    used. On OKX it *succeeds* with margin/derivatives positions only (``[]`` for a spot
    account), which used to hide every spot holding from valuation, exits and close-all."""

    @pytest.mark.asyncio
    async def test_venue_fetch_positions_is_never_consulted(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_positions.return_value = []  # what OKX returns for spot
        mock_client.fetch_balance.return_value = {"total": {"BTC": 1.0}}
        mock_client.create_order.return_value = {
            "id": "B1",
            "status": "closed",
            "average": 100.0,
            "filled": 1.0,
        }
        await executor.place_order("BTC/EUR", OrderSide.BUY, 1.0, price=100.0)

        (pos,) = await executor.get_positions()
        assert (pos.symbol, pos.quantity) == ("BTC/EUR", 1.0)
        mock_client.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_ledger_is_an_empty_book(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        assert await executor.get_positions() == []


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_known_order(self, executor: CcxtExecutor, mock_client: AsyncMock) -> None:
        # First place an order to register the symbol
        mock_client.create_order.return_value = {"id": "order-1", "status": "open"}
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)

        result = await executor.cancel_order("order-1")
        assert result is True
        mock_client.cancel_order.assert_awaited_once_with("order-1", "BTC/USDT")

    @pytest.mark.asyncio
    async def test_cancel_unknown_order(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        result = await executor.cancel_order("nonexistent")
        assert result is False
        mock_client.cancel_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_failure_returns_false(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"id": "order-1", "status": "open"}
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0)

        mock_client.cancel_order.side_effect = Exception("order already filled")
        result = await executor.cancel_order("order-1")
        assert result is False


class TestGetCash:
    @pytest.mark.asyncio
    async def test_returns_balance(self, executor: CcxtExecutor, mock_client: AsyncMock) -> None:
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
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        # Real fetch_free_balance(): currency code → {free, used, total}.
        mock_client.fetch_free_balance.return_value = {
            "BTC": {"free": 0.5, "used": 0.0, "total": 0.5},
            "USDT": {"free": 1234.5, "used": 10.0, "total": 1244.5},
        }
        assert await executor.get_cash() == pytest.approx(1234.5)  # free, not total

    @pytest.mark.asyncio
    async def test_free_balance_total_fallback_and_case_insensitive_key(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_free_balance.return_value = {"usdt": {"total": 77.0}}
        assert await executor.get_cash() == pytest.approx(77.0)

    @pytest.mark.asyncio
    async def test_missing_quote_currency_is_zero(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.fetch_free_balance.return_value = {"BTC": {"free": 1.0}}
        assert await executor.get_cash() == 0.0

    @pytest.mark.asyncio
    async def test_closed_order_records_fill_price_and_time(
        self, executor: CcxtExecutor, mock_client: AsyncMock
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
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"id": "D-OPEN", "status": "open"}
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=1.0)
        assert result.status == "pending"
        assert result.filled_at is None

    @pytest.mark.asyncio
    async def test_fetch_positions_not_supported_degrades_gracefully(
        self, executor: CcxtExecutor, mock_client: AsyncMock
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
        self, executor: CcxtExecutor, mock_client: AsyncMock
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
        self, executor: CcxtExecutor, mock_client: AsyncMock
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
        self, executor: CcxtExecutor, mock_client: AsyncMock
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
    """§7.9: exit levels from entry signals are attached to the ledger positions and
    dropped once the position closes."""

    @pytest.mark.asyncio
    async def test_levels_attached_then_dropped_on_close(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
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
        assert await executor.get_positions() == []


class TestClose:
    """close() must release the keyed exchange's aiohttp session.

    In testnet mode the executor owns a dedicated CCXT exchange client; without
    an explicit close it leaks on shutdown.
    """

    @pytest.mark.asyncio
    async def test_closes_exchange_client(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        await executor.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        await executor.close()
        await executor.close()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_without_close_method_is_noop(self) -> None:
        executor = CcxtExecutor(
            AsyncMock(spec=["create_order"]), quote_currency="USDT", venue="test"
        )
        await executor.close()  # must not raise


class TestReconcileOpenOrders:
    """§7.28: orders left ``open`` at the venue are re-polled and their status
    transitions reported once, flowing through the same FIFO ledger."""

    async def _place_pending(self, executor: CcxtExecutor, mock_client: AsyncMock) -> None:
        mock_client.create_order.return_value = {"id": "D-OPEN", "status": "open"}
        result = await executor.place_order(
            "BTC/USDT",
            OrderSide.BUY,
            quantity=1.0,
            price=100.0,
            decision_id=7,
            stop_loss=95.0,
            take_profit=120.0,
        )
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_pending_order_lands_filled_with_attribution(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        await self._place_pending(executor, mock_client)
        mock_client.fetch_order.return_value = {
            "id": "D-OPEN",
            "status": "closed",
            "average": 102.0,
            "filled": 1.0,
            "updated": 1700000000000,
        }

        updates = await executor.reconcile_open_orders()
        assert len(updates) == 1
        order = updates[0]
        assert order.status == "filled"
        assert order.price == pytest.approx(102.0)
        assert order.filled_at is not None

        # The entry plan survived the pending phase (§7.9 local enforcement).
        assert executor._exit_levels["BTC/USDT"] == (95.0, 120.0)

        # The reconciled buy entered the FIFO ledger with its decision id, so a
        # later closing sell attributes PnL back to decision 7 (§7.8).
        mock_client.create_order.return_value = {
            "id": "D-SELL",
            "status": "closed",
            "filled": 1.0,
            "average": 110.0,
        }
        sell = await executor.place_order("BTC/USDT", OrderSide.SELL, quantity=1.0, price=110.0)
        assert sell.realized_pnl == pytest.approx(8.0)
        assert sell.closed_entries[0].entry_decision_id == 7

        # Two-phase (§7.44): re-delivered until the agent confirms it persisted the
        # transition — without feeding the ledger a second time.
        again = await executor.reconcile_open_orders()
        assert [o.order_id for o in again] == ["D-OPEN"]
        assert executor._tracker.quantity("BTC/USDT") == 0.0  # the sell consumed the only lot
        mock_client.fetch_order.assert_awaited_once()  # no re-poll of a resolved order

        # Confirmed ⇒ tracking dropped; nothing to reconcile anymore.
        executor.confirm_reconciled("D-OPEN")
        assert await executor.reconcile_open_orders() == []

    @pytest.mark.asyncio
    async def test_cancelled_order_reports_and_stops_tracking(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        await self._place_pending(executor, mock_client)
        mock_client.fetch_order.return_value = {"id": "D-OPEN", "status": "canceled"}

        updates = await executor.reconcile_open_orders()
        assert [o.status for o in updates] == ["cancelled"]
        assert executor._tracker.quantity("BTC/USDT") == 0.0
        executor.confirm_reconciled("D-OPEN")
        assert await executor.reconcile_open_orders() == []

    @pytest.mark.asyncio
    async def test_still_open_is_skipped_and_retried(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        await self._place_pending(executor, mock_client)
        mock_client.fetch_order.return_value = {"id": "D-OPEN", "status": "open"}
        assert await executor.reconcile_open_orders() == []

        # Still tracked — a later fill is reported on the next poll.
        mock_client.fetch_order.return_value = {
            "id": "D-OPEN",
            "status": "closed",
            "average": 100.0,
            "filled": 1.0,
        }
        updates = await executor.reconcile_open_orders()
        assert [o.status for o in updates] == ["filled"]

    @pytest.mark.asyncio
    async def test_failed_poll_is_fail_soft_and_retried(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        await self._place_pending(executor, mock_client)
        mock_client.fetch_order.side_effect = TimeoutError("venue unreachable")
        assert await executor.reconcile_open_orders() == []  # no crash, stays tracked

        mock_client.fetch_order.side_effect = None
        mock_client.fetch_order.return_value = {
            "id": "D-OPEN",
            "status": "closed",
            "average": 100.0,
            "filled": 1.0,
        }
        assert [o.status for o in await executor.reconcile_open_orders()] == ["filled"]

    @pytest.mark.asyncio
    async def test_client_without_fetch_order_is_a_noop(self) -> None:
        client = AsyncMock(spec=["create_order", "cancel_order", "fetch_free_balance"])
        executor = CcxtExecutor(client, quote_currency="USDT", venue="test")
        # A list-spec mock is not async-aware; wire the call explicitly.
        client.create_order = AsyncMock(return_value={"id": "D-OPEN", "status": "open"})
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)
        assert await executor.reconcile_open_orders() == []

    @pytest.mark.asyncio
    async def test_cancel_by_us_stops_reconciliation(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        await self._place_pending(executor, mock_client)
        assert await executor.cancel_order("D-OPEN") is True
        # The venue might still echo a late fill, but we no longer track it.
        mock_client.fetch_order.return_value = {
            "id": "D-OPEN",
            "status": "closed",
            "average": 100.0,
        }
        assert await executor.reconcile_open_orders() == []


class TestSpotPositionsFromLedger:
    """§7.41/§7.64: spot holdings come from our own fills, capped by venue balances."""

    @staticmethod
    def _spot_client() -> AsyncMock:
        client = AsyncMock()
        client.fetch_balance.return_value = {"total": {"BTC": 1.0, "EUR": 9_000.0}}
        client.fetch_free_balance.return_value = {"EUR": {"free": 9_000.0}}
        client.create_order.return_value = {
            "id": "B1",
            "status": "closed",
            "average": 100.0,
            "filled": 1.0,
        }
        return client

    async def test_filled_buy_shows_as_a_marked_position(self) -> None:
        client = self._spot_client()
        executor = CcxtExecutor(client, quote_currency="EUR", venue="test")
        await executor.place_order(
            "BTC/USDT", OrderSide.BUY, 1.0, price=100.0, stop_loss=90.0, take_profit=130.0
        )
        executor.update_price("BTC/USDT", 120.0)

        (pos,) = await executor.get_positions()
        assert (pos.symbol, pos.quantity, pos.avg_entry_price, pos.current_price) == (
            "BTC/USDT",
            1.0,
            100.0,
            120.0,
        )
        assert (pos.stop_loss, pos.take_profit) == (90.0, 130.0)

    async def test_buy_no_longer_reads_as_a_loss(self) -> None:
        from src.core.models import PortfolioState

        client = self._spot_client()
        executor = CcxtExecutor(client, quote_currency="EUR", venue="test")
        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, price=100.0)
        executor.update_price("BTC/USDT", 100.0)
        book = PortfolioState(
            cash=await executor.get_cash(), positions=await executor.get_positions()
        )
        # Pre-§7.41 total value = free quote only (9000): the buy looked like −100.
        assert book.total_value == pytest.approx(9_100.0)

    async def test_venue_balance_caps_the_ledger(self) -> None:
        client = self._spot_client()
        client.fetch_balance.return_value = {"total": {"BTC": 0.4}}  # e.g. partly withdrawn
        executor = CcxtExecutor(client, quote_currency="EUR", venue="test")
        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, price=100.0)
        (pos,) = await executor.get_positions()
        assert pos.quantity == pytest.approx(0.4)

    async def test_balance_failure_falls_back_to_the_ledger(self) -> None:
        client = self._spot_client()
        client.fetch_balance.side_effect = TimeoutError("venue slow")
        executor = CcxtExecutor(client, quote_currency="EUR", venue="test")
        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, price=100.0)
        (pos,) = await executor.get_positions()
        assert pos.quantity == pytest.approx(1.0)

    async def test_close_all_now_sells_spot_holdings(self) -> None:
        from src.core.config import RiskSettings
        from src.core.decision_pipeline import DecisionPipeline
        from src.core.risk_engine import RiskEngine

        client = self._spot_client()
        executor = CcxtExecutor(client, quote_currency="EUR", venue="test")
        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, price=100.0)
        executor.update_price("BTC/USDT", 110.0)
        client.create_order.return_value = {
            "id": "S1",
            "status": "closed",
            "average": 110.0,
            "filled": 1.0,
        }
        pipeline = DecisionPipeline(
            provider=AsyncMock(),
            llm_client=AsyncMock(),
            risk_engine=RiskEngine(RiskSettings()),
            executor=executor,
        )

        closed = await pipeline.close_all_positions()

        assert [(s, o.side, o.realized_pnl) for s, o in closed] == [
            ("BTC/USDT", OrderSide.SELL, pytest.approx(10.0))
        ]


class TestPartialFills:
    """§7.61: a cancel/expiry that traded is a partial fill, never a lost one."""

    async def test_cancelled_create_order_with_fill_is_a_partial_fill(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {
            "id": "IOC-1",
            "status": "canceled",
            "filled": 0.4,
            "amount": 1.0,
            "average": 100.0,
        }
        result = await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, price=100.0)
        assert (result.status, result.quantity) == ("filled", 0.4)
        assert result.reason is not None and "partially filled" in result.reason
        assert result.filled_at is not None
        assert executor._tracker.quantity("BTC/USDT") == pytest.approx(0.4)

    async def test_reconciled_cancel_with_fill_feeds_the_ledger(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"id": "P-1", "status": "open", "amount": 1.0}
        await executor.place_order(
            "BTC/USDT", OrderSide.BUY, 1.0, price=100.0, decision_id=3, stop_loss=90.0
        )
        mock_client.fetch_order.return_value = {
            "id": "P-1",
            "status": "canceled",
            "filled": 0.3,
            "amount": 1.0,
            "average": 101.0,
        }
        (update,) = await executor.reconcile_open_orders()
        assert (update.status, update.quantity) == ("filled", 0.3)
        assert executor._tracker.quantity("BTC/USDT") == pytest.approx(0.3)
        assert executor._exit_levels["BTC/USDT"] == (90.0, None)

    async def test_untraded_cancel_stays_cancelled(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"id": "P-2", "status": "open", "amount": 1.0}
        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, price=100.0)
        mock_client.fetch_order.return_value = {"id": "P-2", "status": "canceled", "filled": 0.0}
        (update,) = await executor.reconcile_open_orders()
        assert update.status == "cancelled"
        assert executor._tracker.quantity("BTC/USDT") == 0.0

    async def test_expired_is_terminal_not_pending_forever(
        self, executor: CcxtExecutor, mock_client: AsyncMock
    ) -> None:
        mock_client.create_order.return_value = {"id": "P-3", "status": "open", "amount": 1.0}
        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, price=100.0)
        mock_client.fetch_order.return_value = {"id": "P-3", "status": "expired", "filled": 0}
        (update,) = await executor.reconcile_open_orders()
        assert update.status == "cancelled"
        executor.confirm_reconciled("P-3")
        assert await executor.reconcile_open_orders() == []

    def test_venue_label(self, mock_client: AsyncMock) -> None:
        executor = CcxtExecutor(mock_client, quote_currency="EUR", venue="myokx-sandbox")
        assert executor.venue == "myokx-sandbox"
