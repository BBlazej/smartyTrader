"""Equity doesn't dip while a venue BUY is unbooked (§7.79).

Found on the OKX demo: a BUY filled at the venue, both status polls timed out
(``50004``), and the next snapshot showed cash −460 EUR with no position — a fake
−10 % for the drawdown / daily-loss gates, and a too-low daily baseline if it had
been the day's first snapshot. Cash committed to unbooked BUYs is now part of
equity (``PortfolioState.pending_value``) but never of spendable cash.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.config import RiskSettings
from src.core.decision_pipeline import calculate_quantity
from src.core.models import Action, OrderSide, PortfolioState, TradeSignal
from src.core.portfolio import pending_buy_value, read_portfolio
from src.execution.ccxt_executor import PendingOrderRecord
from tests.unit.test_restart_fees import _agent, _buy
from tests.unit.test_venue_orders import SYMBOL, FakeVenue, _executor

PRICE = 100_000.0
START = 10_000.0


class TimeoutVenue(FakeVenue):
    """Fills at once (cash spent, coins delivered) while status polls time out."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_up = False
        self.locked = 0.0  # cash a resting limit BUY holds (not in the free balance)

    async def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float | None = None
    ) -> dict[str, Any]:
        ack = await super().create_order(symbol, type, side, amount, price)
        order = self.orders[ack["id"]]
        if order["state"] == "fill":
            order["fees"] = self._fill(order, amount, price or PRICE)
            order["booked"] = True
        elif side == "buy":
            self.locked += amount * (price or PRICE)
        return ack

    async def fetch_free_balance(self) -> dict[str, Any]:
        return {"EUR": {"free": self.cash - self.locked}}

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        if not self.api_up:
            raise RuntimeError('myokx {"code":"50004","msg":"API endpoint request timeout. "}')
        return await super().fetch_order(order_id, symbol)

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        order = self.orders[order_id]
        self.locked -= order["amount"] * (order["price"] or PRICE)
        return await super().cancel_order(order_id, symbol)


class TestUnconfirmedFill:
    async def test_equity_holds_while_the_fill_is_unconfirmed(self) -> None:
        venue = TimeoutVenue()
        executor = _executor(venue)
        executor.update_price(SYMBOL, PRICE)
        before = await read_portfolio(executor)

        order = await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=PRICE)
        assert order.status == "pending"  # the confirm poll timed out
        during = await read_portfolio(executor)
        assert during.cash == pytest.approx(START - 100.2)  # the venue spent it
        assert during.positions == []  # the ledger doesn't know yet
        assert during.total_value == pytest.approx(before.total_value)  # …but no dip

        venue.api_up = True
        (update,) = await executor.reconcile_open_orders()
        assert update.status == "filled"
        after = await read_portfolio(executor)
        assert after.pending_value == 0.0  # booked → counted once, as a position
        # Only the real costs remain: the crossed limit and the coin fee.
        assert after.total_value == pytest.approx(START - 100.2 + 0.000999 * PRICE)

    async def test_the_agents_snapshot_does_not_dip(self, tmp_db_path: str) -> None:
        from src.core.storage import Storage

        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        storage.bind_venue("myokx-sandbox")
        try:
            await _agent(storage, _executor(TimeoutVenue()), _buy()).run_cycle()
            snapshot = await storage.get_latest_portfolio_snapshot()
            assert snapshot is not None
            assert snapshot.positions_json == "[]"
            assert snapshot.total_value == pytest.approx(START)  # was START − notional
        finally:
            await storage.close()


class TestRestingLimit:
    async def test_locked_cash_stays_in_equity_until_the_cancel_returns_it(self) -> None:
        venue = TimeoutVenue(rest=frozenset({"buy"}))
        venue.api_up = True
        executor = _executor(venue, order_ttl_seconds=60)
        order = await executor.place_order(SYMBOL, OrderSide.BUY, 0.001, price=PRICE)
        assert order.status == "pending"

        resting = await read_portfolio(executor)
        assert resting.cash == pytest.approx(START - 100.2)  # locked at the venue
        assert resting.total_value == pytest.approx(START)

        assert await executor.cancel_order(order.order_id)
        cancelled = await read_portfolio(executor)
        assert cancelled.pending_value == 0.0
        assert cancelled.total_value == pytest.approx(START)  # cash back, counted once

    def test_reloaded_working_buys_are_valued_at_their_row_price(self) -> None:
        executor = _executor(FakeVenue())
        executor.load_pending_orders(
            [
                PendingOrderRecord("B", SYMBOL, OrderSide.BUY, 0.002, price=PRICE),
                PendingOrderRecord("S", SYMBOL, OrderSide.SELL, 0.002, price=PRICE),
            ]
        )
        assert executor.pending_buy_value() == pytest.approx(200.0)  # SELLs lock coins


class TestPendingValueIsNotSpendable:
    def test_total_value_counts_it_sizing_does_not(self) -> None:
        book = PortfolioState(cash=100.0, pending_value=900.0)
        assert book.total_value == pytest.approx(1_000.0)
        signal = TradeSignal(
            symbol=SYMBOL, action=Action.BUY, confidence=0.9, reasoning="x", stop_loss=90.0
        )
        qty = calculate_quantity(signal, book, RiskSettings(max_position_pct=1.0), 100.0)
        assert qty * 100.0 <= 100.0 + 1e-9  # only the free cash can be spent

    def test_hook_is_optional_and_fail_soft(self) -> None:
        assert pending_buy_value(object()) == 0.0
        assert pending_buy_value(MagicMock()) == 0.0  # non-numeric mock answer

        class Broken:
            def pending_buy_value(self) -> float:
                raise RuntimeError("boom")

        assert pending_buy_value(Broken()) == 0.0
