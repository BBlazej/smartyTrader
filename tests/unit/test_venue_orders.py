"""Venue order lifecycle for the keyed ccxt executor (§7.75, found on the OKX demo).

The fake venue mirrors what the real OKX demo did on 2026-09-28: ``create_order``
acknowledges with an id only (``status: None``), fills are learned from
``fetch_order`` with unified timestamps and fees (BUY in the coin, SELL in EUR), and
amounts are floored to the lot size. Per side it either fills or *rests*.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.core.config import RiskSettings, Settings, VenueOrderSettings
from src.core.decision_pipeline import (
    DecisionPipeline,
    buy_cost_factor,
    sizing_cost_model,
)
from src.core.models import OHLCV, Action, MarketSnapshot, OrderSide, TradeSignal
from src.core.risk_engine import RiskEngine
from src.execution.ccxt_executor import CcxtExecutor, PendingOrderRecord
from src.execution.position_tracker import PositionTracker

SYMBOL = "BTC/EUR"
LOT = 1e-6
MIN_AMOUNT = 1e-4
FILL_MS = 1_790_578_032_555
BUY_FEE_RATE = 0.001  # charged in BTC on buys
SELL_FEE_RATE = 0.001  # charged in EUR on sells


class FakeVenue:
    """OKX-shaped ccxt stand-in with lot-size flooring and per-side fill behaviour."""

    def __init__(self, *, rest: frozenset[str] = frozenset(), cash: float = 10_000.0) -> None:
        self.rest = set(rest)  # sides whose orders stay open until cancelled
        self.markets = {
            SYMBOL: {
                "spot": True,
                "active": True,
                "quote": "EUR",
                "limits": {"amount": {"min": MIN_AMOUNT}},
            }
        }
        self.orders: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        self.cash = cash
        self.base = 0.0
        self.fetch_calls = 0
        self.partial_on_cancel = 0.0

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        floored = math.floor(amount / LOT + 1e-9) * LOT
        if floored <= 0:
            raise ValueError("amount below precision")  # ccxt raises InvalidOrder
        return f"{floored:.6f}"

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{round(price, 1):.1f}"

    async def fetch_balance(self) -> dict[str, Any]:
        return {"total": {"BTC": self.base, "EUR": self.cash}}

    async def fetch_free_balance(self) -> dict[str, Any]:
        return {"EUR": {"free": self.cash}}

    async def fetch_trading_fee(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "maker": 0.0008, "taker": 0.001}

    async def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float | None = None
    ) -> dict[str, Any]:
        order_id = f"O{len(self.orders) + 1}"
        self.orders[order_id] = {
            "type": type,
            "side": side,
            "amount": amount,
            "price": price,
            "state": "open" if side in self.rest else "fill",
        }
        return {"id": order_id, "status": None, "fees": [], "fee": None}

    def _fill(self, order: dict[str, Any], amount: float, px: float) -> list[dict[str, Any]]:
        if order["side"] == "buy":
            fee = amount * BUY_FEE_RATE
            self.base += amount - fee
            self.cash -= amount * px
            return [{"cost": fee, "currency": "BTC"}]
        fee = amount * px * SELL_FEE_RATE
        self.base -= amount
        self.cash += amount * px - fee
        return [{"cost": fee, "currency": "EUR"}]

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.fetch_calls += 1
        order = self.orders[order_id]
        px = order["price"] or 100_000.0
        if order["state"] == "open":
            return {"id": order_id, "status": "open", "filled": 0.0, "amount": order["amount"]}
        if order["state"] == "cancelled":
            filled = self.partial_on_cancel
            fees = self._fill(order, filled, px) if filled and not order.get("booked") else []
            order["booked"] = True
            return {
                "id": order_id,
                "status": "canceled",
                "filled": filled,
                "amount": order["amount"],
                "average": px if filled else None,
                "fees": fees,
                "lastTradeTimestamp": FILL_MS if filled else None,
            }
        fees = [] if order.get("booked") else self._fill(order, order["amount"], px)
        order["booked"] = True
        return {
            "id": order_id,
            "status": "closed",
            "amount": order["amount"],
            "filled": order["amount"],
            "average": px,
            "price": order["price"],
            "fees": fees,
            "timestamp": FILL_MS - 5_000,
            "lastTradeTimestamp": FILL_MS,
            "lastUpdateTimestamp": FILL_MS,
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        self.orders[order_id]["state"] = "cancelled"
        return {"id": order_id}


def _executor(venue: FakeVenue, **policy: Any) -> CcxtExecutor:
    orders = VenueOrderSettings(**{"fill_confirm_delay_seconds": 0, **policy})
    return CcxtExecutor(venue, quote_currency="EUR", venue="myokx-sandbox", orders=orders)


def _age(executor: CcxtExecutor, order_id: str, seconds: float) -> None:
    pending = executor._open_orders[order_id]
    pending.placed_at = (pending.placed_at or datetime.now(UTC)) - timedelta(seconds=seconds)


# ── (f) OKX acknowledges with an id only ──────────────────────────


class TestFillConfirmation:
    async def test_id_only_ack_is_resolved_by_one_poll(self) -> None:
        venue = FakeVenue()
        result = await _executor(venue).place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        assert result.status == "filled"
        assert venue.fetch_calls == 1
        # (d) the venue's trade time, not "now" and not the placement time.
        assert result.filled_at == datetime.fromtimestamp(FILL_MS / 1000, tz=UTC)

    async def test_failed_poll_leaves_it_pending_for_reconciliation(self) -> None:
        venue = FakeVenue()
        venue.fetch_order = AsyncMock(side_effect=RuntimeError("timeout"))  # type: ignore[method-assign]
        executor = _executor(venue)
        result = await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        assert result.status == "pending"
        assert executor.working_order_sides(SYMBOL) == {OrderSide.BUY}

    def test_missing_status_maps_to_pending_explicitly(self) -> None:
        from src.execution.ccxt_executor import _resolve_status

        assert _resolve_status({"id": "1", "status": None}, 1.0)[0] == "pending"

    async def test_reconciled_fill_carries_the_venue_trade_time(self) -> None:
        venue = FakeVenue(rest=frozenset({"buy"}))
        executor = _executor(venue)
        order = await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        venue.orders[order.order_id]["state"] = "fill"
        (update,) = await executor.reconcile_open_orders()
        assert update.status == "filled"
        assert update.filled_at == datetime.fromtimestamp(FILL_MS / 1000, tz=UTC)


# ── (a) marketable order terms ────────────────────────────────────


class TestOrderTerms:
    async def test_buy_is_a_limit_crossed_by_the_entry_offset(self) -> None:
        venue = FakeVenue()
        await _executor(venue).place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        order = venue.orders["O1"]
        assert order["type"] == "limit"
        assert order["price"] == pytest.approx(100_200.0)  # 0.2 % over the close

    async def test_sell_goes_at_market_by_default(self) -> None:
        venue = FakeVenue()
        executor = _executor(venue)
        await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        await executor.place_order(SYMBOL, OrderSide.SELL, 0.000999, price=90_000.0)
        assert venue.orders["O2"]["type"] == "market"
        assert venue.orders["O2"]["price"] is None

    async def test_limit_exit_mode_crosses_below_the_close(self) -> None:
        venue = FakeVenue()
        executor = _executor(venue, exit_order_type="limit", exit_offset_pct=0.005)
        await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        await executor.place_order(SYMBOL, OrderSide.SELL, 0.000999, price=90_000.0)
        assert venue.orders["O2"]["type"] == "limit"
        assert venue.orders["O2"]["price"] == pytest.approx(89_550.0)

    async def test_no_reference_price_stays_a_market_order(self) -> None:
        venue = FakeVenue()
        await _executor(venue).place_order(SYMBOL, OrderSide.BUY, 0.001)
        assert venue.orders["O1"]["type"] == "market"

    def test_sizing_reserves_the_entry_offset(self) -> None:
        executor = _executor(FakeVenue())
        assert executor.buy_price_factor == pytest.approx(1.002)
        assert buy_cost_factor(executor) == pytest.approx(1.002)
        assert sizing_cost_model(executor).slippage_pct == pytest.approx(0.002)

    async def test_cash_bound_buy_fits_the_crossed_limit(self) -> None:
        # All-in sizing: a BUY sized to the last euro still fits at the crossed price.
        from src.core.decision_pipeline import calculate_quantity
        from src.core.models import PortfolioState

        executor = _executor(FakeVenue(cash=100.0))
        signal = TradeSignal(
            symbol=SYMBOL, action=Action.BUY, confidence=0.9, reasoning="x", stop_loss=90_000.0
        )
        qty = calculate_quantity(
            signal,
            PortfolioState(cash=100.0),
            RiskSettings(max_position_pct=1.0),
            100_000.0,
            cost_factor=buy_cost_factor(executor),
            cost_model=sizing_cost_model(executor),
        )
        assert qty * 100_000.0 * 1.002 <= 100.0 + 1e-9


# ── (c) lot size, minimum amount, dust ────────────────────────────


class TestLotSizeAndDust:
    async def test_amount_is_floored_to_the_lot_size(self) -> None:
        venue = FakeVenue()
        await _executor(venue).place_order(SYMBOL, OrderSide.BUY, 0.0012349, price=100_000.0)
        assert venue.orders["O1"]["amount"] == pytest.approx(0.001234)

    async def test_below_the_venue_minimum_is_never_sent(self) -> None:
        venue = FakeVenue()
        result = await _executor(venue).place_order(SYMBOL, OrderSide.BUY, 0.00005, price=100_000.0)
        assert result.status == "rejected"
        assert result.order_id.startswith("rejected-")
        assert venue.orders == {}

    async def test_close_leaves_no_phantom_position(self) -> None:
        venue = FakeVenue()
        executor = _executor(venue)
        await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0, decision_id=3)
        held = (await executor.get_positions())[0].quantity
        assert held == pytest.approx(0.000999)  # net of the BTC fee
        # A ledger remainder the lot size can't express …
        executor._tracker.on_buy(SYMBOL, 4e-7, 100_000.0, decision_id=3)
        sell = await executor.place_order(
            SYMBOL, OrderSide.SELL, executor._tracker.quantity(SYMBOL), price=100_000.0
        )
        assert sell.status == "filled"
        # … is written off: no position, no sleeve owner, no stale exit levels.
        assert await executor.get_positions() == []
        assert executor.entry_decision_ids(SYMBOL) == []
        assert SYMBOL not in executor._exit_levels

    async def test_dust_after_restart_replay_is_not_a_position(self) -> None:
        from src.execution.position_tracker import FillRecord

        venue = FakeVenue()
        venue.base = 5e-7
        executor = _executor(venue)
        executor.load_fills(
            [
                FillRecord(symbol=SYMBOL, side="buy", quantity=0.0010005, price=100_000.0),
                FillRecord(symbol=SYMBOL, side="sell", quantity=0.001, price=100_000.0),
            ]
        )
        assert await executor.get_positions() == []
        assert executor.entry_decision_ids(SYMBOL) == []

    def test_tracker_discard(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("X/EUR", 2.0, 1.0)
        assert tracker.discard("X/EUR") == pytest.approx(2.0)
        assert tracker.quantity("X/EUR") == 0.0
        assert tracker.discard("X/EUR") == 0.0


# ── (e) outcomes net of reported fees ─────────────────────────────


class TestFeesInOutcomes:
    async def test_round_trip_outcome_is_net_of_both_fees(self) -> None:
        venue = FakeVenue()
        executor = _executor(venue)
        await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0, decision_id=1)
        # Flat market (the fake fills a market SELL at 100k): only the fees remain.
        sell = await executor.place_order(SYMBOL, OrderSide.SELL, 0.000999, price=100_000.0)
        buy_price = 100_200.0  # the crossed limit the fake filled at
        paid = 0.001 * buy_price
        received = 0.000999 * 100_000.0 * (1 - SELL_FEE_RATE)
        assert sell.realized_pnl == pytest.approx(received - paid, rel=1e-6)
        assert sell.closed_entries[0].entry_decision_id == 1
        assert sell.closed_entries[0].pnl == pytest.approx(sell.realized_pnl)
        # The ledger's outcome matches the cash that actually moved.
        assert venue.cash - 10_000.0 == pytest.approx(sell.realized_pnl, rel=1e-6)

    async def test_quote_fee_on_a_buy_joins_the_cost_basis(self) -> None:
        executor = _executor(FakeVenue())
        pnl, _ = executor._record_fill(
            SYMBOL, OrderSide.BUY, 1.0, 100.0, None, {"fees": [{"currency": "EUR", "cost": 0.5}]}
        )
        assert pnl is None
        pnl, _ = executor._record_fill(SYMBOL, OrderSide.SELL, 1.0, 100.0, None, {})
        assert pnl == pytest.approx(-0.5)

    async def test_trading_fee_reports_the_account_tier(self) -> None:
        rates = await _executor(FakeVenue()).trading_fee(SYMBOL)
        assert rates == {"maker": 0.0008, "taker": 0.001}

    async def test_trading_fee_is_optional(self) -> None:
        venue = FakeVenue()
        venue.fetch_trading_fee = AsyncMock(side_effect=RuntimeError("no"))  # type: ignore[method-assign]
        assert await _executor(venue).trading_fee(SYMBOL) is None


# ── (b) resting orders: TTL + no stacking ─────────────────────────


class TestOrderTtl:
    async def test_order_past_its_ttl_is_cancelled_and_reported(self) -> None:
        venue = FakeVenue(rest=frozenset({"buy"}))
        executor = _executor(venue, order_ttl_seconds=60)
        order = await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        assert await executor.reconcile_open_orders() == []  # young: left working

        _age(executor, order.order_id, 61)
        (update,) = await executor.reconcile_open_orders()
        assert venue.cancelled == [order.order_id]
        assert update.status == "cancelled"
        assert "order_ttl_seconds" in (update.reason or "")
        executor.confirm_reconciled(order.order_id)
        assert executor.working_order_sides(SYMBOL) == set()

    async def test_ttl_cancel_that_traded_books_the_partial_fill(self) -> None:
        venue = FakeVenue(rest=frozenset({"buy"}))
        venue.partial_on_cancel = 0.0005
        executor = _executor(venue, order_ttl_seconds=60)
        order = await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        _age(executor, order.order_id, 61)
        (update,) = await executor.reconcile_open_orders()
        assert update.status == "filled"
        assert update.quantity == pytest.approx(0.0005)
        assert executor._tracker.quantity(SYMBOL) == pytest.approx(0.0005 * (1 - BUY_FEE_RATE))

    async def test_zero_ttl_never_cancels(self) -> None:
        venue = FakeVenue(rest=frozenset({"buy"}))
        executor = _executor(venue, order_ttl_seconds=0)
        order = await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        _age(executor, order.order_id, 86_400)
        assert await executor.reconcile_open_orders() == []
        assert venue.cancelled == []

    def test_reloaded_orders_keep_their_age(self) -> None:
        executor = _executor(FakeVenue())
        naive = datetime(2026, 9, 28, 6, 0, tzinfo=UTC).replace(tzinfo=None)  # SQLite: naive UTC
        executor.load_pending_orders(
            [
                PendingOrderRecord("A", SYMBOL, OrderSide.BUY, 0.001, placed_at=naive),
                PendingOrderRecord("B", SYMBOL, OrderSide.SELL, 0.001),
            ]
        )
        assert executor._open_orders["A"].placed_at == naive.replace(tzinfo=UTC)
        assert executor._open_orders["B"].placed_at is not None  # TTL starts at load

    async def test_restart_rehydration_passes_the_row_age(self, tmp_path: Path) -> None:
        from src.core.rehydration import rehydrate_venue_executor
        from src.core.storage import Storage

        storage = Storage(str(tmp_path / "t.db"), agent="crypto")
        await storage.initialize()
        try:
            await storage.save_order("V-9", SYMBOL, "buy", 0.001, 100_000.0, "pending")
            (row,) = await storage.get_pending_orders()
            executor = _executor(FakeVenue())
            await rehydrate_venue_executor(executor, storage)
            placed = executor._open_orders["V-9"].placed_at
            assert placed is not None
            assert placed.replace(tzinfo=None) == row.created_at.replace(tzinfo=None)
        finally:
            await storage.close()


class TestNoStackedOrders:
    """A resting order on a side is never joined by another one (§7.75 b)."""

    @staticmethod
    def _snapshot(price: float) -> MarketSnapshot:
        candles = [
            OHLCV(open=price, high=price + 1, low=price - 1, close=price, volume=1.0)
            for _ in range(3)
        ]
        return MarketSnapshot(symbol=SYMBOL, timeframe="1h", candles=candles)

    def _pipeline(
        self, executor: CcxtExecutor, prices: list[float], signals: list[TradeSignal]
    ) -> DecisionPipeline:
        provider = AsyncMock()
        provider.fetch_snapshot = AsyncMock(side_effect=[self._snapshot(p) for p in prices])
        llm = AsyncMock()
        llm.ask_trade_signal = AsyncMock(side_effect=signals)
        return DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=RiskEngine(RiskSettings(max_position_pct=0.5)),
            executor=executor,
        )

    @staticmethod
    def _buy() -> TradeSignal:
        return TradeSignal(
            symbol=SYMBOL, action=Action.BUY, confidence=0.9, reasoning="in", stop_loss=95_000.0
        )

    async def test_resting_exit_is_not_resent_until_its_ttl_cancels_it(self) -> None:
        venue = FakeVenue(rest=frozenset({"sell"}))
        executor = _executor(venue, exit_order_type="limit", order_ttl_seconds=60)
        pipeline = self._pipeline(
            executor, [100_000.0, 90_000.0, 88_000.0, 87_000.0], [self._buy()]
        )

        entry = await pipeline.run(SYMBOL)
        assert entry.executed
        first_exit = await pipeline.run(SYMBOL)  # stop breached → SELL rests
        assert first_exit.auto_exit and first_exit.order_result.status == "pending"

        again = await pipeline.run(SYMBOL)  # still breached, SELL still working
        assert again.auto_exit and again.order_result is None
        assert again.skip_reason == "exit order already working at the venue"
        assert [o["side"] for o in venue.orders.values()] == ["buy", "sell"]

        # The TTL cancels it; the next cycle re-places at the fresh mark.
        _age(executor, first_exit.order_result.order_id, 61)
        (cancelled,) = await executor.reconcile_open_orders()
        assert cancelled.status == "cancelled"
        executor.confirm_reconciled(cancelled.order_id)
        retry = await pipeline.run(SYMBOL)
        assert retry.order_result is not None
        assert [o["side"] for o in venue.orders.values()] == ["buy", "sell", "sell"]

    async def test_entry_is_not_stacked_on_a_working_buy(self) -> None:
        venue = FakeVenue(rest=frozenset({"buy"}))
        executor = _executor(venue)
        pipeline = self._pipeline(executor, [100_000.0, 100_000.0], [self._buy(), self._buy()])

        first = await pipeline.run(SYMBOL)
        assert first.order_result is not None and first.order_result.status == "pending"
        second = await pipeline.run(SYMBOL)
        assert second.order_result is None
        assert second.skip_reason == "buy order already working at the venue"
        assert len(venue.orders) == 1

    async def test_close_all_skips_a_position_whose_close_is_working(self) -> None:
        venue = FakeVenue(rest=frozenset({"sell"}))
        executor = _executor(venue)
        await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=100_000.0)
        executor.update_price(SYMBOL, 100_000.0)
        pipeline = self._pipeline(executor, [], [])
        first = await pipeline.close_all_positions()
        assert len(first) == 1 and first[0][1].status == "pending"
        assert await pipeline.close_all_positions() == []
        assert [o["side"] for o in venue.orders.values()] == ["buy", "sell"]


# ── Config ────────────────────────────────────────────────────────


class TestVenueOrderSettings:
    def test_shipped_policy(self) -> None:
        orders = Settings().venue_orders
        assert orders.exit_order_type == "market"
        assert 0 < orders.entry_offset_pct < 0.01
        assert orders.order_ttl_seconds > 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"exit_order_type": "stop"},
            {"entry_offset_pct": -0.001},
            {"exit_offset_pct": 0.05},
            {"order_ttl_seconds": -1},
            {"fill_confirm_delay_seconds": -0.5},
        ],
    )
    def test_invalid_values_are_rejected(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="venue_orders"):
            VenueOrderSettings(**kwargs)
