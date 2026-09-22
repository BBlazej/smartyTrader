"""Unit tests for the real xAPI client (§7.16) — zero network.

Every transaction runs over :class:`FakeTransport`, a scripted stand-in for the
WebSocket: each queued item is either a canned response dict or an exception
raised on ``receive``. This covers login, order payload shape, fill-status
polling, position marking, reconnect-once and balance parsing.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.execution.xtb_client import XApiClient, XTBError

LOGIN_OK: dict[str, Any] = {"status": True, "returnData": {}, "streamSessionId": "s-1"}


class FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.sent: list[dict[str, Any]] = []
        self.connects = 0
        self.closes = 0

    async def connect(self) -> None:
        self.connects += 1

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def receive_json(self, timeout: float) -> dict[str, Any]:
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closes += 1


def _client(transport: FakeTransport) -> XApiClient:
    return XApiClient(
        "42",
        "v3r1fy",
        account_type="demo",
        request_interval_seconds=0.0,
        poll_delay_seconds=0.0,
        transport=transport,
    )


class TestLogin:
    async def test_login_sent_before_first_command(self) -> None:
        t = FakeTransport([LOGIN_OK, {"status": True, "returnData": {"balance": 5_000.0}}])
        assert await _client(t).get_balance() == 5_000.0
        assert [p["command"] for p in t.sent] == ["login", "getMarginLevel"]

    async def test_login_arguments_and_order(self) -> None:
        t = FakeTransport([LOGIN_OK, {"status": True, "returnData": {"balance": 1.0}}] * 2)
        client = _client(t)
        await client.get_balance()
        await client.get_balance()

        # login once, then one getMarginLevel per call — ordered transactions.
        assert t.sent[0] == {
            "command": "login",
            "arguments": {"userId": "42", "password": "v3r1fy"},
        }
        assert [p["command"] for p in t.sent[1:]] == ["getMarginLevel", "getMarginLevel"]
        assert t.connects == 1

    async def test_login_failure_raises_and_stays_logged_out(self) -> None:
        t = FakeTransport([{"status": False, "errorCode": "500", "errorDescr": "bad code"}])
        client = _client(t)
        with pytest.raises(XTBError, match="bad code"):
            await client.get_balance()
        assert not client._logged_in


class TestCreateOrder:
    async def test_buy_payload_and_fill_mapping(self) -> None:
        t = FakeTransport(
            [
                LOGIN_OK,
                {"status": True, "returnData": {"order": 123}},
                {"status": True, "returnData": {"requestStatus": 3}},
            ]
        )
        result = await _client(t).create_order("AAPL", "buy", 2.0, price=210.5)

        order_cmd = t.sent[1]
        assert order_cmd["command"] == "tradeTransaction"
        info = order_cmd["arguments"]["tradeTransInfo"]
        assert info["cmd"] == 0 and info["type"] == 0
        assert info["symbol"] == "AAPL"
        assert info["price"] == 210.5 and info["volume"] == 2.0
        assert result == {
            "order_id": "123",
            "status": "filled",
            "quantity": 2.0,
            "price": 210.5,
        }

    async def test_sell_uses_sell_cmd(self) -> None:
        t = FakeTransport(
            [
                LOGIN_OK,
                {"status": True, "returnData": {"order": 7}},
                {"status": True, "returnData": {"requestStatus": 5}},
            ]
        )
        result = await _client(t).create_order("AAPL", "sell", 1.0, price=200.0)
        assert t.sent[1]["arguments"]["tradeTransInfo"]["cmd"] == 1
        assert result["status"] == "filled"

    async def test_price_none_falls_back_to_symbol_spec_ask(self) -> None:
        t = FakeTransport(
            [
                LOGIN_OK,
                {"status": True, "returnData": {"bid": 99.0, "ask": 100.5}},
                {"status": True, "returnData": {"order": 8}},
                {"status": True, "returnData": {"requestStatus": 3}},
            ]
        )
        result = await _client(t).create_order("MSFT", "buy", 1.0)
        assert t.sent[1]["command"] == "getSymbol"
        assert t.sent[2]["arguments"]["tradeTransInfo"]["price"] == 100.5
        assert result["price"] == 100.5

    async def test_venue_rejection_returns_rejected_dict(self) -> None:
        t = FakeTransport([LOGIN_OK, {"status": False, "errorCode": "6", "errorDescr": "no funds"}])
        result = await _client(t).create_order("AAPL", "buy", 1.0, price=10.0)
        assert result == {"order_id": "", "status": "rejected", "quantity": 1.0}

    async def test_status_polls_until_pending_exhausted(self) -> None:
        responses: list[Any] = [LOGIN_OK, {"status": True, "returnData": {"order": 9}}]
        responses += [{"status": True, "returnData": {"requestStatus": 1}}] * 5
        t = FakeTransport(responses)
        result = await _client(t).create_order("AAPL", "buy", 1.0, price=10.0)
        assert result["status"] == "pending"

    async def test_status_polling_reaches_terminal_late(self) -> None:
        t = FakeTransport(
            [
                LOGIN_OK,
                {"status": True, "returnData": {"order": 9}},
                {"status": True, "returnData": {"requestStatus": 1}},
                {"status": True, "returnData": {"requestStatus": 4}},
            ]
        )
        result = await _client(t).create_order("AAPL", "buy", 1.0, price=10.0)
        assert result["status"] == "rejected"


class TestPositionsAndBalance:
    async def test_positions_mapped_with_live_marks(self) -> None:
        t = FakeTransport(
            [
                LOGIN_OK,
                {
                    "status": True,
                    "returnData": [
                        {"symbol": "AAPL", "volume": 3.0, "open_price": 100.0, "cmd": 0},
                        {"symbol": "TSLA", "volume": 1.0, "open_price": 200.0, "cmd": 1},
                        {"volume": 5.0},  # malformed record without a symbol → skipped
                    ],
                },
                {
                    "status": True,
                    "returnData": {
                        "quotations": [
                            {"symbol": "AAPL", "bid": 110.0, "ask": 110.5},
                            {"symbol": "TSLA", "bid": 190.0, "ask": 191.0},
                        ]
                    },
                },
            ]
        )
        positions = await _client(t).get_positions()

        assert len(positions) == 2
        long_pos, short_pos = positions
        assert long_pos["avg_entry_price"] == 100.0
        assert long_pos["current_price"] == 110.0  # longs mark at the bid
        assert short_pos["current_price"] == 191.0  # shorts mark at the ask
        # §7.38: direction from the opening cmd rides along in the payload.
        assert long_pos["side"] == "long"
        assert short_pos["side"] == "short"

    async def test_positions_without_quotes_fall_back_gracefully(self) -> None:
        t = FakeTransport(
            [
                LOGIN_OK,
                {
                    "status": True,
                    "returnData": [{"symbol": "AAPL", "volume": 1.0, "open_price": 50.0}],
                },
                {"status": True, "returnData": {"quotations": []}},
            ]
        )
        positions = await _client(t).get_positions()
        assert positions[0]["current_price"] is None

    async def test_balance_parsed_from_margin_level(self) -> None:
        t = FakeTransport([LOGIN_OK, {"status": True, "returnData": {"balance": 12_345.6}}])
        assert await _client(t).get_balance() == 12_345.6


class TestResilience:
    async def test_socket_error_triggers_single_reconnect_and_retry(self) -> None:
        t = FakeTransport(
            [
                LOGIN_OK,
                TimeoutError("boom"),  # first attempt of getMarginLevel
                LOGIN_OK,  # re-login after reconnect
                {"status": True, "returnData": {"balance": 42.0}},  # retry succeeds
            ]
        )
        client = _client(t)
        assert await client.get_balance() == 42.0
        assert t.connects == 2
        commands = [p["command"] for p in t.sent]
        assert commands == ["login", "getMarginLevel", "login", "getMarginLevel"]

    async def test_second_socket_error_raises(self) -> None:
        t = FakeTransport([LOGIN_OK, TimeoutError("boom"), LOGIN_OK, TimeoutError("boom again")])
        with pytest.raises(XTBError, match="getMarginLevel"):
            await _client(t).get_balance()

    async def test_close_logs_out_once_and_is_idempotent(self) -> None:
        t = FakeTransport([LOGIN_OK, {"status": True, "returnData": {"balance": 1.0}}])
        client = _client(t)
        await client.get_balance()
        await client.close()
        await client.close()

        assert t.sent[-1] == {"command": "logout"}
        assert t.closes >= 1
        # No further traffic after close.
        await client.close()
        assert [p["command"] for p in t.sent].count("logout") == 1


class TestConfigGuards:
    def test_invalid_account_type_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="demo.*real"):
            XApiClient("1", "x", account_type="live")

    def test_url_follows_host_and_account_type(self) -> None:
        client = XApiClient("1", "x", host="wss://example.test/", account_type="demo")
        assert client.url == "wss://example.test/demo"
