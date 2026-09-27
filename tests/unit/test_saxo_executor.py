"""Saxo OpenAPI client + executor (§7.66), fully mocked — no network.

The client is exercised over ``httpx.MockTransport`` against the documented request /
response shapes; the executor over a fake client covering the Executor contract:
account + instrument resolution (never guessed), whole-share long-only orders,
one-currency sizing, venue fill prices with the §7.62 sanity check, ledger-capped
positions, per-cycle reconciliation (§7.28/§7.44) and restart hooks (§7.58).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.core.config import SaxoExecutionSettings
from src.core.models import OrderSide
from src.execution.ccxt_executor import PendingOrderRecord
from src.execution.position_tracker import FillRecord
from src.execution.saxo_client import LIVE_BASE_URL, SIM_BASE_URL, SaxoApiError, SaxoClient
from src.execution.saxo_executor import SaxoExecutor, _resolve_activity

TOKEN = "s3cr3t-token"

# ── Client ────────────────────────────────────────────────────


def _client(handler) -> SaxoClient:  # type: ignore[no-untyped-def]
    return SaxoClient(TOKEN, transport=httpx.MockTransport(handler))


class TestSaxoClient:
    async def test_bases_and_validation(self) -> None:
        assert SaxoClient(TOKEN).base_url == SIM_BASE_URL
        assert SaxoClient(TOKEN, environment="live").base_url == LIVE_BASE_URL
        with pytest.raises(ValueError, match="sim"):
            SaxoClient(TOKEN, environment="demo")
        with pytest.raises(ValueError, match="token"):
            SaxoClient("")

    async def test_market_order_body_and_auth(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"OrderId": 49036233})

        client = _client(handler)
        order_id = await client.place_market_order(
            account_key="AK", uic=211, buy_sell="Buy", amount=3
        )
        await client.close()
        assert order_id == "49036233"
        assert seen["url"] == f"{SIM_BASE_URL}/trade/v2/orders"
        assert seen["auth"] == f"Bearer {TOKEN}"
        assert seen["body"] == {
            "AccountKey": "AK",
            "Uic": 211,
            "AssetType": "Stock",
            "BuySell": "Buy",
            "Amount": 3,
            "OrderType": "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
        }

    async def test_errors_carry_saxo_message_never_the_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"ErrorInfo": {"ErrorCode": "InsufficientCash", "Message": "No cash"}}
            )

        client = _client(handler)
        with pytest.raises(SaxoApiError) as err:
            await client.place_market_order(account_key="AK", uic=1, buy_sell="Buy", amount=1)
        await client.close()
        assert err.value.status_code == 400
        assert "InsufficientCash" in str(err.value) and TOKEN not in str(err.value)

    async def test_error_info_in_a_200_body_is_an_error(self) -> None:
        client = _client(
            lambda r: httpx.Response(200, json={"ErrorInfo": {"ErrorCode": "X", "Message": "m"}})
        )
        with pytest.raises(SaxoApiError, match="rejected"):
            await client.place_market_order(account_key="AK", uic=1, buy_sell="Buy", amount=1)
        await client.close()

    async def test_missing_order_id_is_an_error(self) -> None:
        client = _client(lambda r: httpx.Response(200, json={}))
        with pytest.raises(SaxoApiError, match="OrderId"):
            await client.place_market_order(account_key="AK", uic=1, buy_sell="Buy", amount=1)
        await client.close()

    async def test_transport_failure_is_wrapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        client = _client(handler)
        with pytest.raises(SaxoApiError, match="ConnectError"):
            await client.get_accounts()
        await client.close()

    async def test_reads_unwrap_data_and_params(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            path = request.url.path
            if path.endswith("/accounts/me"):
                return httpx.Response(200, json={"Data": [{"AccountKey": "AK"}]})
            if path.endswith("/balances"):
                return httpx.Response(200, json={"CashBalance": 1000.0})
            if path.endswith("/instruments"):
                return httpx.Response(200, json={"Data": [{"Identifier": 211}]})
            if path.endswith("/netpositions/me"):
                return httpx.Response(200, json={"Data": [{"NetPositionId": "x"}]})
            if path.endswith("/orderactivities"):
                return httpx.Response(200, json={"Data": [{"Status": "FinalFill"}]})
            return httpx.Response(204)

        client = _client(handler)
        assert await client.get_accounts() == [{"AccountKey": "AK"}]
        assert await client.get_balance("AK", "CK") == {"CashBalance": 1000.0}
        assert await client.find_instruments("AAPL") == [{"Identifier": 211}]
        assert await client.get_net_positions() == [{"NetPositionId": "x"}]
        assert await client.get_order_activity("7") == {"Status": "FinalFill"}
        await client.cancel_order("7", "AK")
        await client.close()
        params = [dict(c.url.params) for c in calls]
        assert params[1] == {"AccountKey": "AK", "ClientKey": "CK"}
        assert params[2] == {"Keywords": "AAPL", "AssetTypes": "Stock"}
        assert params[3] == {"FieldGroups": "NetPositionBase,NetPositionView"}
        assert params[4] == {"OrderId": "7", "EntryType": "Last"}
        assert calls[5].method == "DELETE" and calls[5].url.path.endswith("/trade/v2/orders/7")


# ── Executor ──────────────────────────────────────────────────


class FakeSaxo:
    """In-memory Saxo: one USD account, instruments, fills scripted per order."""

    def __init__(self) -> None:
        self.accounts = [
            {"AccountKey": "AK-USD", "ClientKey": "CK", "Currency": "USD", "AccountId": "1-USD"},
            {"AccountKey": "AK-EUR", "ClientKey": "CK", "Currency": "EUR", "AccountId": "1-EUR"},
        ]
        self.instruments = {
            "AAPL": [
                {"Identifier": 211, "Symbol": "AAPL:xnas", "CurrencyCode": "USD"},
                {"Identifier": 9999, "Symbol": "AAPL:xmil", "CurrencyCode": "EUR"},
            ],
            "MSFT": [{"Identifier": 261, "Symbol": "MSFT:xnas", "CurrencyCode": "USD"}],
            "SAP": [{"Identifier": 500, "Symbol": "SAP:xetr", "CurrencyCode": "EUR"}],
        }
        self.activities: list[dict | None] = []  # served in order, last one repeats
        self.orders: list[dict] = []
        self.net: list[dict] = []
        self.cash = 50_000.0
        self.closed = False

    async def get_accounts(self):
        return self.accounts

    async def get_balance(self, account_key, client_key=None):
        return {"CashBalance": self.cash, "Currency": "USD"}

    async def find_instruments(self, keyword, asset_type="Stock"):
        return self.instruments.get(keyword, [])

    async def get_net_positions(self):
        return self.net

    async def place_market_order(self, **kwargs):
        self.orders.append(kwargs)
        return str(1000 + len(self.orders))

    async def get_order_activity(self, order_id):
        if not self.activities:
            return None
        return self.activities.pop(0) if len(self.activities) > 1 else self.activities[0]

    async def cancel_order(self, order_id, account_key):
        self.cancelled = (order_id, account_key)

    async def close(self):
        self.closed = True


def _fill(amount: float, price: float) -> dict:
    return {"Status": "FinalFill", "FilledAmount": amount, "AveragePrice": price}


def _executor(fake: FakeSaxo, **kwargs) -> SaxoExecutor:  # type: ignore[no-untyped-def]
    kwargs.setdefault("account_currency", "USD")
    kwargs.setdefault("symbol_map", {"AAPL": "AAPL:xnas"})
    return SaxoExecutor(fake, venue="saxo-sim", fill_poll_delays=(0, 0), **kwargs)  # type: ignore[arg-type]


class TestResolveActivity:
    def test_statuses(self) -> None:
        assert _resolve_activity(None, 5)[0] == "pending"
        assert _resolve_activity({"Status": "Placed"}, 5)[0] == "pending"
        assert _resolve_activity(_fill(5, 10.0), 5) == ("filled", 5.0, 10.0, None)
        partial = _resolve_activity(
            {"Status": "Cancelled", "FilledAmount": 2, "AveragePrice": 9.5}, 5
        )
        assert partial[:3] == ("filled", 2.0, 9.5) and "partially" in partial[3]
        assert _resolve_activity({"Status": "Rejected", "SubStatus": "x"}, 5)[0] == "rejected"
        assert _resolve_activity({"Status": "Expired"}, 5)[0] == "cancelled"


class TestSaxoExecutor:
    async def test_buy_fills_at_venue_price_and_tracks_the_lot(self) -> None:
        fake = FakeSaxo()
        fake.activities = [{"Status": "Placed"}, _fill(3, 187.5)]
        ex = _executor(fake)
        order = await ex.place_order(
            "AAPL", OrderSide.BUY, 3.7, price=188.0, decision_id=7, stop_loss=180, take_profit=200
        )
        assert order.status == "filled" and order.quantity == 3 and order.price == 187.5
        assert fake.orders[0] == {
            "account_key": "AK-USD", "uic": 211, "buy_sell": "Buy", "amount": 3.0,
            "asset_type": "Stock",
        }  # fmt: skip
        assert ex.entry_decision_ids("AAPL") == [7]
        fake.net = [
            {
                "NetPositionBase": {"Uic": 211, "Amount": 3, "AssetType": "Stock"},
                "NetPositionView": {"CurrentPrice": 190.0},
            }
        ]
        (pos,) = await ex.get_positions()
        assert (pos.quantity, pos.avg_entry_price, pos.current_price) == (3, 187.5, 190.0)
        assert (pos.stop_loss, pos.take_profit) == (180, 200)
        ex.update_price("AAPL", 191.0)
        assert (await ex.get_positions())[0].current_price == 191.0

    async def test_sell_realizes_pnl_and_is_capped_to_the_ledger(self) -> None:
        fake = FakeSaxo()
        ex = _executor(fake)
        fake.activities = [_fill(3, 100.0)]
        await ex.place_order("AAPL", OrderSide.BUY, 3, price=100.0, decision_id=1)
        fake.activities = [_fill(3, 110.0)]
        sell = await ex.place_order("AAPL", OrderSide.SELL, 99, price=110.0, decision_id=2)
        assert fake.orders[-1]["amount"] == 3 and fake.orders[-1]["buy_sell"] == "Sell"
        assert sell.realized_pnl == pytest.approx(30.0)
        assert [e.entry_decision_id for e in sell.closed_entries] == [1]
        assert await ex.get_positions() == []

    async def test_refusals_never_reach_the_venue(self) -> None:
        fake = FakeSaxo()
        ex = _executor(fake)
        short = await ex.place_order("AAPL", OrderSide.SELL, 1, price=100.0)
        assert short.status == "rejected" and "short" in short.reason
        tiny = await ex.place_order("AAPL", OrderSide.BUY, 0.4, price=100.0)
        assert tiny.status == "rejected" and "rounds to zero" in tiny.reason
        fx = await ex.place_order("SAP", OrderSide.BUY, 5, price=100.0)
        assert fx.status == "rejected" and "EUR" in fx.reason
        assert fake.orders == []

    async def test_instrument_and_account_are_never_guessed(self) -> None:
        fake = FakeSaxo()
        ex = _executor(fake, symbol_map={})
        with pytest.raises(RuntimeError, match="ambiguous"):
            await ex.place_order("AAPL", OrderSide.BUY, 1, price=100.0)  # xnas vs xmil
        fake.activities = [_fill(1, 400.0)]
        assert (await ex.place_order("MSFT", OrderSide.BUY, 1, price=400.0)).status == "filled"

        with pytest.raises(RuntimeError, match="account"):
            await _executor(FakeSaxo(), account_currency=None).get_cash()
        keyed = _executor(FakeSaxo(), account_key="AK-EUR", account_currency=None)
        assert await keyed.get_cash() == 50_000.0

    async def test_implausible_fill_price_books_the_request(self) -> None:
        fake = FakeSaxo()
        fake.activities = [_fill(2, 1.0)]  # a 99 % gap — a units/scale bug, not a fill
        order = await _executor(fake).place_order("AAPL", OrderSide.BUY, 2, price=100.0)
        assert order.price == 100.0

    async def test_working_order_is_reconciled_two_phase(self) -> None:
        fake = FakeSaxo()
        fake.activities = [{"Status": "Placed"}]
        ex = _executor(fake)
        order = await ex.place_order("AAPL", OrderSide.BUY, 2, price=100.0, decision_id=5)
        assert order.status == "pending" and ex.pending_decision_id(order.order_id) == 5
        assert await ex.reconcile_open_orders() == []  # still working

        fake.activities = [_fill(2, 101.0)]
        (first,) = await ex.reconcile_open_orders()
        assert first.status == "filled" and first.price == 101.0
        (again,) = await ex.reconcile_open_orders()  # re-delivered until confirmed
        assert again is first
        ex.confirm_reconciled(order.order_id)
        assert await ex.reconcile_open_orders() == []
        assert ex.entry_decision_ids("AAPL") == [5]  # ledger fed exactly once

    async def test_restart_hooks(self) -> None:
        fake = FakeSaxo()
        ex = _executor(fake)
        counts = ex.load_fills(
            [FillRecord("AAPL", "buy", 4, 100.0, decision_id=3, stop_loss=90, take_profit=120)]
        )
        assert counts == {"replayed_fills": 1, "open_symbols": 1}
        fake.net = [{"NetPositionBase": {"Uic": 211, "Amount": 2, "AssetType": "Stock"}}]
        (pos,) = await ex.get_positions()
        assert pos.quantity == 2 and pos.stop_loss == 90  # capped by what Saxo holds
        assert ex.load_pending_orders([PendingOrderRecord("77", "AAPL", OrderSide.SELL, 2)]) == 1
        fake.activities = [{"Status": "Cancelled"}]
        (resolved,) = await ex.reconcile_open_orders()
        assert resolved.status == "cancelled"

    async def test_net_position_failure_falls_back_to_the_ledger(self) -> None:
        fake = FakeSaxo()
        ex = _executor(fake)
        ex.load_fills([FillRecord("AAPL", "buy", 4, 100.0)])

        async def boom():
            raise SaxoApiError("503")

        fake.get_net_positions = boom  # type: ignore[method-assign]
        assert (await ex.get_positions())[0].quantity == 4

    async def test_cancel_and_close(self) -> None:
        fake = FakeSaxo()
        ex = _executor(fake)
        assert await ex.cancel_order("9") is True and fake.cancelled == ("9", "AK-USD")
        await ex.close()
        await ex.close()
        assert fake.closed and ex.venue == "saxo-sim"


# ── Config + runner wiring ────────────────────────────────────


class TestSaxoConfig:
    def test_defaults_and_validation(self) -> None:
        cfg = SaxoExecutionSettings()
        assert (cfg.enabled, cfg.environment, cfg.account_currency) == (False, "sim", "USD")
        with pytest.raises(ValueError, match="environment"):
            SaxoExecutionSettings(environment="demo")
        with pytest.raises(ValueError, match="one-to-one"):
            SaxoExecutionSettings(symbol_map={"A": "X:xnas", "B": "X:xnas"})
        with pytest.raises(ValueError, match="fill_poll_delays"):
            SaxoExecutionSettings(fill_poll_delays=[])
        with pytest.raises(ValueError, match="amount_decimals"):
            SaxoExecutionSettings(amount_decimals=9)

    def test_only_one_stocks_venue(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from src.core.config import Settings

        raw = (Path(__file__).parents[2] / "config" / "settings.yaml").read_text()
        raw = raw.replace("xtb_execution:\n  enabled: false", "xtb_execution:\n  enabled: true")
        raw = raw.replace("saxo_execution:\n  enabled: false", "saxo_execution:\n  enabled: true")
        config = tmp_path / "settings.yaml"
        config.write_text(raw)
        with pytest.raises(ValueError, match="at most one stocks venue"):
            Settings(str(config))

    def test_shipped_config_is_off(self) -> None:
        from src.core.config import Settings

        settings = Settings()
        assert settings.saxo_execution.enabled is False
        assert settings.saxo_execution.environment == "sim"


class TestRunnerWiring:
    def _settings(self, **cfg) -> SimpleNamespace:  # type: ignore[no-untyped-def]
        return SimpleNamespace(saxo_execution=SaxoExecutionSettings(enabled=True, **cfg))

    def test_needs_the_token(self) -> None:
        from scripts.run_stocks_agent import _saxo_executor

        log = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            assert _saxo_executor(self._settings(), log) is None
        assert "SAXO_ACCESS_TOKEN" in log.warning.call_args.args[0]

    def test_sim_with_token_builds_the_executor(self) -> None:
        from scripts.run_stocks_agent import _saxo_executor

        with patch.dict(os.environ, {"SAXO_ACCESS_TOKEN": TOKEN}, clear=True):
            executor = _saxo_executor(self._settings(symbol_map={"AAPL": "AAPL:xnas"}), MagicMock())
        assert isinstance(executor, SaxoExecutor) and executor.venue == "saxo-sim"
        assert executor.client.base_url == SIM_BASE_URL

    def test_live_needs_the_ack(self) -> None:
        from scripts.run_stocks_agent import _saxo_executor

        with patch.dict(os.environ, {"SAXO_ACCESS_TOKEN": TOKEN}, clear=True):
            assert _saxo_executor(self._settings(environment="live"), MagicMock()) is None
        env = {"SAXO_ACCESS_TOKEN": TOKEN, "LIVE_TRADING_ACK": "I_ACCEPT_REAL_MONEY_RISK"}
        with patch.dict(os.environ, env, clear=True):
            live = _saxo_executor(self._settings(environment="live"), MagicMock())
        assert live is not None and live.venue == "saxo-live"

    def test_disabled_or_absent_is_none(self) -> None:
        from scripts.run_stocks_agent import _saxo_executor

        assert _saxo_executor(SimpleNamespace(), MagicMock()) is None
        off = SimpleNamespace(saxo_execution=SaxoExecutionSettings())
        assert _saxo_executor(off, MagicMock()) is None
