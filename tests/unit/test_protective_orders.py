"""Venue-side protective orders (§7.34): an OCO per open position, against a fake OKX.

The fake follows what the OKX demo did (2026-10-01): an ``oco``/``conditional`` algo
order freezes the coins, firing makes it ``effective`` with a filled child market
order, and cancelling a fired one raises ``OrderNotFound``.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.config import VenueOrderSettings
from src.core.models import OrderSide
from src.execution.ccxt_executor import CcxtExecutor
from src.execution.position_tracker import FillRecord
from src.execution.protective_orders import CLIENT_ID_PREFIX

SYMBOL = "BTC/EUR"
PROTECT = VenueOrderSettings(fill_confirm_delay_seconds=0, protective_orders=True)


class OrderNotFound(Exception):
    pass


class FakeOKX:
    """Spot orders fill at once; algo orders live until fired or cancelled."""

    def __init__(self) -> None:
        self.algos: dict[str, dict[str, Any]] = {}
        self.children: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []
        self.history: list[dict[str, Any]] = []
        self.fail_algo_placement = 0
        self.market_price = 100.0  # where a market SELL fills
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return str(self._n)

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = params or {}
        self.calls.append(("create", type, side, amount, dict(params)))
        if type in ("oco", "conditional"):
            if self.fail_algo_placement:
                self.fail_algo_placement -= 1
                raise RuntimeError("51008 insufficient balance")
            algo_id = "A" + self._id()
            self.algos[algo_id] = {
                "id": algo_id,
                "kind": type,
                "symbol": symbol,
                "amount": amount,
                "params": dict(params),
                "info": {"state": "live", "algoClOrdId": params.get("algoClOrdId", "")},
            }
            return {"id": algo_id, "status": None}
        fill = price or self.market_price  # a BUY limit fills at its limit (close × 1.002)
        return {"id": "O" + self._id(), "status": "closed", "filled": amount, "average": fill}

    async def cancel_order(self, id, symbol, params=None):
        self.calls.append(("cancel", id, dict(params or {})))
        algo = self.algos.get(id)
        if algo is None or algo["info"]["state"] != "live":
            raise OrderNotFound("51400 Order cancellation failed")
        algo["info"]["state"] = "canceled"
        return {"id": id}

    async def fetch_order(self, id, symbol, params=None):
        if params and params.get("trigger"):
            return self.algos[id]
        return self.children[id]

    async def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        kind = (params or {}).get("ordType", "trigger")  # ccxt's default — never ours
        return [
            a
            for a in self.algos.values()
            if a["info"]["state"] == "live" and a.get("kind", "oco") == kind
        ]

    async def fetch_closed_orders(self, symbol, since=None, limit=None, params=None):
        kind = (params or {}).get("ordType", "trigger")
        return [a for a in self.history if a.get("kind", "oco") == kind]

    async def fetch_balance(self, params=None):
        return {"total": {"BTC": 100.0}}

    async def fetch_free_balance(self, params=None):
        return {"EUR": 10_000.0}

    def fire(self, algo_id: str, side_hit: str = "sl", price: float = 90.0) -> str:
        """The venue triggers ``algo_id``: a filled market child appears."""
        algo = self.algos[algo_id]
        child_id = "C" + self._id()
        algo["info"].update({"state": "effective", "ordId": child_id, "actualSide": side_hit})
        self.children[child_id] = {
            "id": algo_id,  # ccxt reports the parent algo id on the child (seen on OKX)
            "status": "closed",
            "filled": algo["amount"],
            "average": price,
            "fee": {"cost": 0.18, "currency": "EUR"},
            "lastTradeTimestamp": 1_790_000_000_000,
        }
        return child_id

    def live_algos(self) -> list[dict[str, Any]]:
        return [a for a in self.algos.values() if a["info"]["state"] == "live"]


@pytest.fixture()
def okx() -> FakeOKX:
    return FakeOKX()


@pytest.fixture()
def executor(okx: FakeOKX) -> CcxtExecutor:
    return CcxtExecutor(okx, quote_currency="EUR", venue="okx-test", orders=PROTECT)


async def buy(executor: CcxtExecutor, qty: float = 1.0, sl: float = 95.0, tp: float = 110.0):
    return await executor.place_order(
        SYMBOL, OrderSide.BUY, qty, price=100.0, decision_id=7, stop_loss=sl, take_profit=tp
    )


class TestLifecycle:
    async def test_a_filled_buy_gets_an_oco_for_the_whole_position(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        await buy(executor)
        [algo] = okx.live_algos()
        create = next(c for c in okx.calls if c[1] == "oco")
        assert create[2] == "sell" and create[3] == pytest.approx(1.0)
        params = create[4]
        assert params["stopLossPrice"] == 95.0 and params["takeProfitPrice"] == 110.0
        assert params["tdMode"] == "cash"  # the default margin mode is refused (51010)
        assert params["algoClOrdId"].startswith(CLIENT_ID_PREFIX)
        assert algo["amount"] == pytest.approx(1.0)

    async def test_only_a_stop_is_a_conditional_order(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        await executor.place_order(SYMBOL, OrderSide.BUY, 1.0, price=100.0, stop_loss=95.0)
        assert [c[1] for c in okx.calls if c[0] == "create"] == ["limit", "conditional"]

    async def test_adding_to_a_position_resizes_the_oco(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        await buy(executor)
        await buy(executor, qty=0.5, sl=96.0, tp=112.0)
        [algo] = okx.live_algos()
        assert algo["amount"] == pytest.approx(1.5)
        assert algo["params"]["stopLossPrice"] == 96.0  # the latest entry's levels

    async def test_an_agent_sell_cancels_the_oco_first(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        await buy(executor)  # entry 100.2: the limit at close × (1 + 0.2 %)
        okx.market_price = 105.0
        result = await executor.place_order(SYMBOL, OrderSide.SELL, 1.0, price=105.0)
        kinds = [c[0] + ":" + str(c[1]) for c in okx.calls]
        assert kinds.index("cancel:A2") < kinds.index("create:market")  # coins unfrozen first
        assert result.status == "filled" and result.realized_pnl == pytest.approx(4.8)
        assert okx.live_algos() == []  # flat → nothing re-placed

    async def test_a_partial_sell_leaves_an_oco_for_the_rest(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        await buy(executor)
        await executor.place_order(SYMBOL, OrderSide.SELL, 0.4, price=105.0)
        [algo] = okx.live_algos()
        assert algo["amount"] == pytest.approx(0.6)

    async def test_off_by_default(self, okx: FakeOKX) -> None:
        plain = CcxtExecutor(
            okx,
            quote_currency="EUR",
            venue="okx-test",
            orders=VenueOrderSettings(fill_confirm_delay_seconds=0),
        )
        await buy(plain)
        assert okx.algos == {}


class TestFiring:
    async def test_fired_while_running_is_booked_by_reconciliation(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        await buy(executor)
        child = okx.fire("A2", "sl", price=94.0)
        [result] = await executor.reconcile_open_orders()
        assert result.order_id == child and result.status == "filled"
        assert result.reason == "venue stop-loss"
        assert result.realized_pnl == pytest.approx(94.0 - 100.2 - 0.18)  # move + the sell fee
        assert [e.entry_decision_id for e in result.closed_entries] == [7]
        assert await executor.get_positions() == []
        assert okx.live_algos() == []
        # Two-phase: re-delivered until confirmed, then gone.
        assert await executor.reconcile_open_orders() == [result]
        executor.confirm_reconciled(child)
        assert await executor.reconcile_open_orders() == []

    async def test_a_sell_racing_a_fired_oco_sells_nothing_more(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        await buy(executor)
        child = okx.fire("A2", "tp", price=111.0)
        result = await executor.place_order(SYMBOL, OrderSide.SELL, 1.0, price=111.0)
        assert result.status == "rejected"  # the venue already sold it: nothing tracked
        assert not any(c[0] == "create" and c[1] == "market" for c in okx.calls)
        [booked] = await executor.reconcile_open_orders()
        assert booked.order_id == child and booked.reason == "venue take-profit"
        assert booked.realized_pnl == pytest.approx(111.0 - 100.2 - 0.18)

    async def test_a_failed_placement_is_retried_next_cycle(
        self, executor: CcxtExecutor, okx: FakeOKX
    ) -> None:
        okx.fail_algo_placement = 1
        await buy(executor)
        assert okx.live_algos() == []
        await executor.reconcile_open_orders()
        assert len(okx.live_algos()) == 1

    async def test_a_dead_oco_is_replaced(self, executor: CcxtExecutor, okx: FakeOKX) -> None:
        await buy(executor)
        okx.algos["A2"]["info"]["state"] = "canceled"  # e.g. cancelled by hand
        await executor.reconcile_open_orders()
        [algo] = okx.live_algos()
        assert algo["id"] != "A2"


class TestRestart:
    def fills(self, order_id: str = "O1") -> list[FillRecord]:
        return [
            FillRecord(
                symbol=SYMBOL,
                side="buy",
                quantity=1.0,
                price=100.0,
                decision_id=7,
                stop_loss=95.0,
                take_profit=110.0,
                order_id=order_id,
            )
        ]

    async def test_fired_while_down_is_booked_and_live_ones_replaced(self, okx: FakeOKX) -> None:
        # Before the restart: an OCO that fired while we were down, and one stale live one.
        okx.algos["A9"] = {
            "id": "A9",
            "symbol": SYMBOL,
            "amount": 1.0,
            "params": {},
            "info": {"state": "live", "algoClOrdId": CLIENT_ID_PREFIX + "x"},
        }
        child = okx.fire("A9", "sl", price=93.0)
        okx.history = [okx.algos["A9"]]
        okx.algos["A8"] = {
            "id": "A8",
            "symbol": SYMBOL,
            "amount": 1.0,
            "params": {},
            "info": {"state": "live", "algoClOrdId": CLIENT_ID_PREFIX + "y"},
        }
        foreign = {"id": "F1", "info": {"state": "live", "algoClOrdId": "someone-else"}}
        okx.algos["F1"] = {**foreign, "symbol": SYMBOL, "amount": 1.0, "params": {}}

        restarted = CcxtExecutor(okx, quote_currency="EUR", venue="okx-test", orders=PROTECT)
        restarted.load_fills(self.fills())
        counts = await restarted.restore_protection()

        assert counts["fired_while_down"] == 1
        [booked] = await restarted.reconcile_open_orders()
        assert booked.order_id == child and booked.realized_pnl == pytest.approx(-7.18)
        assert okx.algos["A8"]["info"]["state"] == "canceled"  # ours, stale
        assert okx.algos["F1"]["info"]["state"] == "live"  # not ours — never touched
        assert counts["placed"] == 0  # flat after the stop → nothing to protect

    async def test_an_already_stored_fill_is_not_booked_twice(self, okx: FakeOKX) -> None:
        okx.algos["A9"] = {
            "id": "A9",
            "symbol": SYMBOL,
            "amount": 1.0,
            "params": {},
            "info": {"state": "live", "algoClOrdId": CLIENT_ID_PREFIX + "x"},
        }
        child = okx.fire("A9")
        okx.history = [okx.algos["A9"]]
        restarted = CcxtExecutor(okx, quote_currency="EUR", venue="okx-test", orders=PROTECT)
        restarted.load_fills(
            [
                *self.fills(),
                FillRecord(SYMBOL, "sell", 1.0, 90.0, order_id=child),  # stored last run
            ]
        )
        counts = await restarted.restore_protection()
        assert counts["fired_while_down"] == 0
        assert await restarted.reconcile_open_orders() == []

    async def test_an_open_position_gets_a_fresh_oco(self, okx: FakeOKX) -> None:
        restarted = CcxtExecutor(okx, quote_currency="EUR", venue="okx-test", orders=PROTECT)
        restarted.load_fills(self.fills())
        counts = await restarted.restore_protection()
        assert counts == {"fired_while_down": 0, "placed": 1}
        [algo] = okx.live_algos()
        assert algo["params"]["stopLossPrice"] == 95.0
