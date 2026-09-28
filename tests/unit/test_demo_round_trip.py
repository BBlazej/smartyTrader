"""Unit tests for the OKX demo round-trip smoke script (§7.28).

The fake client reproduces the payload shapes the real OKX demo returned on
2026-09-28: ``create_order`` answers with an id and ``status: None`` (resolved only
by ``fetch_order``), buy fees are charged in the base currency, sell fees in EUR.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.demo_round_trip import RawRecorder, ensure_demo, round_trip, settle
from src.core.config import VenueOrderSettings
from src.core.models import OHLCV, MarketSnapshot, OrderResult, OrderSide
from src.execution.ccxt_executor import CcxtExecutor

SYMBOL = "BTC/EUR"
CLOSE = 73_111.8
BUY_FEE_BTC = 5.471e-7


class FakeOkxDemo:
    """OKX-demo-shaped ccxt stand-in; ``rest=True`` leaves every order open."""

    isSandboxModeEnabled = True

    def __init__(self, *, rest: bool = False) -> None:
        self.rest = rest
        self.orders: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        self.base = 1.0  # the demo account's pre-loaded BTC
        self.cash = 4600.0

    async def load_markets(self) -> dict[str, Any]:
        return {
            SYMBOL: {
                "spot": True,
                "active": True,
                "quote": "EUR",
                "limits": {"amount": {"min": 1e-5}, "cost": {"min": None}},
            }
        }

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"bid": CLOSE - 5, "ask": CLOSE + 13, "last": CLOSE}

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{int(amount * 1e8) / 1e8:.8f}"

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{round(price, 1):.1f}"

    async def fetch_balance(self) -> dict[str, Any]:
        return {"total": {"BTC": self.base, "EUR": self.cash}}

    async def fetch_free_balance(self) -> dict[str, Any]:
        return {"EUR": {"free": self.cash}}

    async def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float | None = None
    ) -> dict[str, Any]:
        order_id = str(len(self.orders) + 1)
        self.orders[order_id] = {"side": side, "amount": amount, "price": price}
        return {"id": order_id, "status": None, "fees": [], "fee": None}  # OKX: id only

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        order = self.orders[order_id]
        if self.rest:
            return {"id": order_id, "status": "open", "filled": 0.0, "amount": order["amount"]}
        amount = round(order["amount"], 6)  # the demo truncates to its lot size
        if order["side"] == "buy":
            self.base += amount - BUY_FEE_BTC
            self.cash -= amount * CLOSE
            fee = {"cost": BUY_FEE_BTC, "currency": "BTC"}
        else:
            self.base -= amount
            self.cash += amount * CLOSE * 0.998
            fee = {"cost": amount * CLOSE * 0.002, "currency": "EUR"}
        return {
            "id": order_id,
            "status": "closed",
            "amount": amount,
            "filled": amount,
            "average": CLOSE,
            "fees": [fee],
            "lastTradeTimestamp": 1_790_578_032_555,
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        return {"id": order_id}


class FakeProvider:
    async def fetch_snapshot(self, symbol: str, timeframe: str = "1h") -> MarketSnapshot:
        candle = OHLCV(open=CLOSE, high=CLOSE, low=CLOSE, close=CLOSE, volume=1.0)
        return MarketSnapshot(symbol=symbol, timeframe=timeframe, candles=[candle])


def _executor(client: FakeOkxDemo) -> CcxtExecutor:
    return CcxtExecutor(
        client,
        quote_currency="EUR",
        venue="myokx-sandbox",
        orders=VenueOrderSettings(fill_confirm_delay_seconds=0),
    )


async def _run(client: FakeOkxDemo, *, execute: bool = True) -> dict:
    executor = _executor(client)
    return await round_trip(
        executor,
        FakeProvider(),
        RawRecorder(client),
        symbol=SYMBOL,
        notional=20.0,
        timeframe="1h",
        timeout=0.05,
        poll=0.01,
        execute=execute,
    )


class TestEnsureDemo:
    @pytest.mark.parametrize("mode", ["paper", "myokx-LIVE"])
    def test_refuses_non_sandbox_modes(self, mode: str) -> None:
        with pytest.raises(SystemExit, match="refusing"):
            ensure_demo(mode, FakeOkxDemo())

    def test_refuses_client_not_in_sandbox(self) -> None:
        client = FakeOkxDemo()
        client.isSandboxModeEnabled = False
        with pytest.raises(SystemExit, match="not in sandbox"):
            ensure_demo("myokx-sandbox", client)

    def test_accepts_sandbox(self) -> None:
        ensure_demo("myokx-sandbox", FakeOkxDemo())


class TestRoundTrip:
    async def test_dry_run_places_nothing(self) -> None:
        client = FakeOkxDemo()
        report = await _run(client, execute=False)
        assert report["dry_run"] is True
        assert report["buy_quantity"] == pytest.approx(20.0 / CLOSE, abs=1e-8)
        assert client.orders == {}

    async def test_okx_shaped_round_trip_reconciles_and_books_net(self) -> None:
        client = FakeOkxDemo()
        report = await _run(client)

        assert report["outcome"] == "round trip complete"
        # OKX answers create_order without a status; the executor's post-placement
        # fetch_order resolves the fill in the same call (§7.75 f).
        assert report["buy_initial_status"] == "filled"
        assert report["buy"]["status"] == "filled"
        # The ledger books the BUY net of the base-currency fee (§7.65) …
        bought = report["buy"]["quantity"]
        assert report["ledger_after_buy"] == pytest.approx(bought - BUY_FEE_BTC)
        assert report["base_delta_after_buy"] == pytest.approx(report["ledger_after_buy"])
        # … and the SELL closes the whole ledger position with a net outcome (§7.75 e):
        # flat price, so the loss is exactly the two fees.
        assert client.orders["2"]["amount"] == pytest.approx(report["ledger_after_buy"], abs=1e-8)
        sold = client.orders["2"]["amount"]
        fees = (
            BUY_FEE_BTC * CLOSE * (sold / report["ledger_after_buy"])
            + round(sold, 6) * CLOSE * 0.002
        )
        assert report["sell"]["realized_pnl"] == pytest.approx(-fees, rel=1e-3)
        assert report["sell"]["venue_fees"][0]["currency"] == "EUR"

    async def test_orders_follow_the_venue_policy(self) -> None:
        # §7.75 a: the executor crosses the BUY limit and sends the SELL at market.
        client = FakeOkxDemo()
        report = await _run(client)
        assert client.orders["1"]["price"] == pytest.approx(CLOSE * 1.002, abs=0.1)
        assert report["buy_sent"]["type"] == "limit"
        assert report["sell_sent"] == {
            "type": "market",
            "side": "sell",
            "amount": report["sell_sent"]["amount"],
            "price": "None",
        }

    async def test_lot_size_dust_is_written_off(self) -> None:
        # §7.75 c: the demo fills the SELL at its lot size; the sliver never stays a position.
        client = FakeOkxDemo()
        report = await _run(client)
        assert report["ledger_after_sell"] == 0.0

    async def test_resting_buy_is_cancelled_and_nothing_sold(self) -> None:
        client = FakeOkxDemo(rest=True)
        report = await _run(client)
        assert report["buy"]["status"] == "pending"
        assert "resting" in report["buy"]["reason"]
        assert client.cancelled == ["1"]
        assert len(client.orders) == 1  # no SELL placed
        assert report["outcome"].startswith("BUY did not fill")

    async def test_below_venue_minimum_refuses(self) -> None:
        client = FakeOkxDemo()
        executor = _executor(client)
        with pytest.raises(SystemExit, match="below the venue minimum"):
            await round_trip(
                executor,
                FakeProvider(),
                RawRecorder(client),
                symbol=SYMBOL,
                notional=0.5,
                timeframe="1h",
                timeout=0.05,
                poll=0.01,
                execute=True,
            )


class TestSettle:
    async def test_terminal_order_returned_unchanged(self) -> None:
        order = OrderResult(
            order_id="x", symbol=SYMBOL, side=OrderSide.BUY, quantity=1.0, status="filled"
        )
        assert await settle(_executor(FakeOkxDemo()), order, timeout=0.05, poll=0.01) is order
