"""A real XTB xAPI client (§7.16) — the WebSocket implementation of the
:class:`src.execution.xtb_executor.XTBClient` seam.

Protocol reality (verified against maintained wrappers, Sept 2026)
------------------------------------------------------------------
XTB's xAPI discontinued the ``ws.xtb.com`` / ``xapi.xtb.com`` hosts on
2025-03-14; trading now lives on ``wss://ws.xapi.pro/{demo,real}`` (plus a
``.../demoStream`` variant we deliberately do **not** use — the agent polls).
Contrary to the original PLAN wording ("OAuth2 flow"), there is no OAuth2
token endpoint: authentication is xAPI's classic ``login`` command sent over
the socket with the account id and the **xAPI verification code** generated in
xStation settings (valid for 30 days, revocable). Requests are plain ordered
JSON transactions on the main socket: send one command object, receive its
response — no request ids.

Design notes
------------
* The socket lives behind :class:`XTBTransport` so every branch (login, order
  mapping, status polling, reconnect, balance) is unit-testable with a fake —
  zero network in tests, per project rules. ``websockets`` is imported lazily
  inside the real transport so this module loads without it.
* Orders are **instant** (``type=OPEN``, cmd BUY/SELL at the current mark);
  the agent never places pending orders, which keeps ``cancel_order`` a rarely
  exercised path (it issues xAPI's DELETE transaction).
* **Closing is its own transaction (§7.40).** ``cmd=SELL, type=OPEN`` does *not*
  close a long — it opens a short. A position is closed with ``type=CLOSE``, the
  *opening* ``cmd`` and the trade's ``order`` number (``getTrades``);
  :meth:`XApiClient.close_trade` does that and the executor routes every reducing
  order through it.
* Fills are confirmed by polling ``tradeTransactionStatus`` briefly; an order
  still in flight returns ``pending`` and is treated like paper's accepted flow.
* Positions carry live marks fetched via ``getTickPrices`` (bid for longs, ask
  for shorts) so unrealized PnL is real even without the streaming channel.
* **Volume caveat:** xAPI sizes positions in *lots* (``volume``); for XTB
  equities one lot is typically one share, but symbol specs vary — sizing is
  left as-is and flagged in the docs. Commission arrives on trade records, not
  ``create_order``, so tracked fills stay gross of fees (§7.8 precedent).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol

import structlog

logger = structlog.get_logger()

DEFAULT_HOST = "wss://ws.xapi.pro"

# xAPI trade enums (grounded against maintained wrappers).
_CMD_BUY = 0
_CMD_SELL = 1
_TYPE_OPEN = 0
_TYPE_CLOSE = 2
_TYPE_DELETE = 4

#: ``tradeTransactionStatus.requestStatus`` → our stable order statuses — the
#: documented REQUEST_STATUS set (ERROR 0, PENDING 1, ACCEPTED 3, REJECTED 4; §7.40
#: re-grounding — codes 2/5/6 used to be mapped but do not exist). Unknown codes
#: stay ``pending`` (polled until the budget runs out), never a guessed fill.
_REQUEST_STATUS_MAP: dict[int, str] = {
    0: "rejected",  # ERROR
    1: "pending",  # PENDING
    3: "filled",  # ACCEPTED — executed
    4: "rejected",  # REJECTED
}


class XTBError(RuntimeError):
    """xAPI returned ``status: false`` (or the socket failed repeatedly)."""

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class XTBTransport(Protocol):
    """Minimal socket seam: ordered JSON transactions."""

    async def connect(self) -> None: ...

    async def send_json(self, payload: dict[str, Any]) -> None: ...

    async def receive_json(self, timeout: float) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class WebSocketXTBTransport:
    """Real transport on top of ``websockets`` (imported lazily)."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._conn: Any | None = None

    async def connect(self) -> None:
        import websockets  # lazy: keeps this module importable without the dep

        self._conn = await websockets.connect(self._url, close_timeout=2, max_size=None)

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._conn is None:
            raise XTBError("transport not connected")
        await self._conn.send(json.dumps(payload))

    async def receive_json(self, timeout: float) -> dict[str, Any]:
        if self._conn is None:
            raise XTBError("transport not connected")
        raw = await asyncio.wait_for(self._conn.recv(), timeout)
        return json.loads(raw)  # type: ignore[arg-type]

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()


class XApiClient:
    """Implements the four-method ``XTBClient`` protocol over xAPI transactions.

    Pass ``transport=`` to inject a fake (or the real
    :class:`WebSocketXTBTransport`) — otherwise one is built lazily from
    ``host``/``account_type`` on the first command. Login runs over whatever
    transport is connected, so auth failures are testable too.
    """

    def __init__(
        self,
        account_id: str,
        verification_code: str,
        *,
        host: str = DEFAULT_HOST,
        account_type: str = "demo",
        timeout_seconds: float = 10.0,
        request_interval_seconds: float = 0.3,
        status_polls: int = 5,
        poll_delay_seconds: float = 0.5,
        transport: XTBTransport | None = None,
    ) -> None:
        if account_type not in ("demo", "real"):
            raise ValueError("account_type must be 'demo' or 'real'")
        self._account_id = account_id
        self._verification_code = verification_code
        self._url = f"{host.rstrip('/')}/{account_type}"
        self._timeout = timeout_seconds
        self._min_interval = request_interval_seconds
        self._status_polls = max(1, status_polls)
        self._poll_delay = poll_delay_seconds
        self._transport = transport or WebSocketXTBTransport(self._url)
        self._logged_in = False
        self._last_request: float | None = None
        self._lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return self._url

    # ── transaction core ──────────────────────────────────────

    async def _login(self) -> None:
        await self._transport.connect()
        response = await self._raw(
            {
                "command": "login",
                "arguments": {"userId": self._account_id, "password": self._verification_code},
            }
        )
        if response.get("status") is not True:
            raise XTBError(
                f"xAPI login failed: {response.get('errorDescr')}",
                error_code=str(response.get("errorCode")),
            )
        self._logged_in = True

    async def _raw(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._transport.send_json(payload)
        return await self._transport.receive_json(self._timeout)

    async def _command(self, command: str, arguments: dict[str, Any] | None = None) -> Any:
        """One ordered transaction; reconnects + retries once on socket errors.

        xAPI rate-limits to ~5 requests/second; ``request_interval_seconds``
        spaces outgoing commands so bursts (order → status poll → positions)
        never trip the limiter.
        """
        async with self._lock:
            if not self._logged_in:
                await self._login()

            payload = {"command": command}
            if arguments is not None:
                payload["arguments"] = arguments

            for attempt in (1, 2):
                if self._last_request is not None:
                    elapsed = time.monotonic() - self._last_request
                    if elapsed < self._min_interval:
                        await asyncio.sleep(self._min_interval - elapsed)
                try:
                    response = await self._raw(payload)
                    self._last_request = time.monotonic()
                    break
                except (XTBError, TimeoutError, ConnectionError, OSError) as exc:
                    logger.warning(
                        "xAPI transaction failed",
                        command=command,
                        attempt=attempt,
                        error=str(exc),
                    )
                    if attempt == 2:
                        raise XTBError(f"xAPI command '{command}' failed: {exc}") from exc
                    self._logged_in = False
                    await self._reset_transport()
                    await self._login()

            status = response.get("status")
            if status is False:
                raise XTBError(
                    str(response.get("errorDescr") or f"xAPI command '{command}' rejected"),
                    error_code=str(response.get("errorCode")),
                )
            return response.get("returnData")

    async def _reset_transport(self) -> None:
        try:
            await self._transport.close()
        except Exception as exc:  # noqa: BLE001 - best-effort teardown before reconnect
            logger.debug("xAPI transport close failed (continuing)", error=str(exc))

    async def _await_fill_status(self, order_id: int) -> str:
        """Poll ``tradeTransactionStatus`` until terminal (or polls exhausted)."""
        mapped = "pending"
        for attempt in range(self._status_polls):
            data = await self._command("tradeTransactionStatus", {"order": order_id}) or {}
            mapped = _REQUEST_STATUS_MAP.get(int(data.get("requestStatus", 1)), "pending")
            if mapped != "pending":
                return mapped
            if attempt < self._status_polls - 1:
                await asyncio.sleep(self._poll_delay)
        return mapped

    # ── the XTBClient protocol (consumed by XTBExecutor) ──────

    async def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float | None = None,
    ) -> dict[str, Any]:
        """Instant market-style order (xAPI ``tradeTransaction`` type=OPEN)."""
        cmd = _CMD_BUY if side == "buy" else _CMD_SELL
        if price is None:
            spec = await self._command("getSymbol", {"symbol": symbol}) or {}
            price = float(spec.get("ask") if cmd == _CMD_BUY else spec.get("bid"))

        try:
            data = await self._command(
                "tradeTransaction",
                {
                    "tradeTransInfo": {
                        "cmd": cmd,
                        "customComment": "trading-agent",
                        "expiration": 0,
                        "offset": 0,
                        "order": 0,
                        "price": float(price),
                        "sl": 0,
                        "symbol": symbol,
                        "tp": 0,
                        "type": _TYPE_OPEN,
                        "volume": float(quantity),
                    }
                },
            )
        except XTBError as exc:
            # A venue-side rejection is data for the audit trail, not an
            # exception the pipeline should crash on.
            logger.warning("xAPI order rejected", symbol=symbol, side=side, error=str(exc))
            return {"order_id": "", "status": "rejected", "quantity": float(quantity)}

        order_id = int(data.get("order", 0))
        status = await self._await_fill_status(order_id)
        return {
            "order_id": str(order_id),
            "status": status,
            "quantity": float(quantity),
            "price": float(price),
        }

    async def get_open_trades(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open trades (``getTrades`` openedOnly) with what closing needs (§7.40).

        Each record: ``order`` (the number ``type=CLOSE`` must reference), ``symbol``,
        ``cmd`` (0 = long / BUY-opened, 1 = short / SELL-opened), ``volume`` (lots)
        and ``open_price``. Oldest first, so closes consume trades FIFO.
        """
        records = await self._command("getTrades", {"openedOnly": True}) or []
        trades = [
            {
                "order": int(r.get("order", 0)),
                "symbol": str(r.get("symbol") or ""),
                "cmd": int(r.get("cmd", 0)),
                "volume": float(r.get("volume", 0.0)),
                "open_price": float(r.get("open_price", 0.0)),
            }
            for r in records
            if r.get("symbol") and (symbol is None or r.get("symbol") == symbol)
        ]
        return sorted(trades, key=lambda t: t["order"])

    async def close_trade(
        self,
        order: int,
        symbol: str,
        cmd: int,
        volume: float,
        price: float | None = None,
    ) -> dict[str, Any]:
        """Close (part of) an open trade — xAPI ``tradeTransaction`` ``type=CLOSE`` (§7.40).

        ``cmd`` is the trade's *opening* command and ``order`` its number from
        :meth:`get_open_trades`; ``volume`` below the trade's volume closes it
        partially. Without a price, a long closes at the bid and a short at the ask.
        Same result shape as :meth:`create_order`; venue rejections come back as
        ``status: rejected`` rather than raising.
        """
        if price is None:
            spec = await self._command("getSymbol", {"symbol": symbol}) or {}
            price = float(spec.get("bid") if cmd == _CMD_BUY else spec.get("ask"))
        try:
            data = await self._command(
                "tradeTransaction",
                {
                    "tradeTransInfo": {
                        "cmd": cmd,
                        "customComment": "trading-agent close",
                        "expiration": 0,
                        "offset": 0,
                        "order": int(order),
                        "price": float(price),
                        "sl": 0,
                        "symbol": symbol,
                        "tp": 0,
                        "type": _TYPE_CLOSE,
                        "volume": float(volume),
                    }
                },
            )
        except XTBError as exc:
            logger.warning("xAPI close rejected", symbol=symbol, order=order, error=str(exc))
            return {"order_id": "", "status": "rejected", "quantity": float(volume)}
        close_order = int(data.get("order", 0))
        status = await self._await_fill_status(close_order)
        return {
            "order_id": str(close_order),
            "status": status,
            "quantity": float(volume),
            "price": float(price),
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Delete a pending order (xAPI ``tradeTransaction`` type=DELETE).

        The agent only places instant orders, so this path is rarely exercised;
        the executor already converts failures into ``False``.
        """
        return await self._command(
            "tradeTransaction",
            {
                "tradeTransInfo": {
                    "cmd": _CMD_BUY,
                    "customComment": "",
                    "expiration": 0,
                    "offset": 0,
                    "order": int(order_id),
                    "price": 0,
                    "sl": 0,
                    "symbol": "",
                    "tp": 0,
                    "type": _TYPE_DELETE,
                    "volume": 0,
                }
            },
        )

    async def get_positions(self) -> list[dict[str, Any]]:
        """Open positions (``getTrades`` openedOnly) marked with live quotes."""
        records = await self._command("getTrades", {"openedOnly": True}) or []
        symbols = sorted({str(r.get("symbol")) for r in records if r.get("symbol")})
        marks: dict[str, tuple[float, float]] = {}
        if symbols:
            # level=0 + timestamp=0 returns the most recent quotations.
            data = (
                await self._command(
                    "getTickPrices", {"level": 0, "symbols": symbols, "timestamp": 0}
                )
                or {}
            )
            for quote in data.get("quotations", []):
                marks[str(quote.get("symbol"))] = (
                    float(quote.get("bid", 0.0)),
                    float(quote.get("ask", 0.0)),
                )

        positions: list[dict[str, Any]] = []
        for record in records:
            symbol = str(record.get("symbol") or "")
            if not symbol:
                continue
            bid, ask = marks.get(symbol, (0.0, 0.0))
            # Long positions mark at the bid, shorts at the ask.
            current = bid if int(record.get("cmd", 0)) == _CMD_BUY else ask
            positions.append(
                {
                    "symbol": symbol,
                    "quantity": float(record.get("volume", 0.0)),
                    # xAPI keeps volume positive; direction is the opening cmd
                    # (§7.38) — expose it so the executor maps shorts honestly.
                    "side": "long" if int(record.get("cmd", 0)) == _CMD_BUY else "short",
                    "avg_entry_price": float(record.get("open_price", 0.0)),
                    "current_price": current or None,
                    "profit": float(record.get("profit", 0.0) or 0.0),
                }
            )
        return positions

    async def get_balance(self) -> float:
        data = await self._command("getMarginLevel") or {}
        return float(data.get("balance", 0.0))

    async def close(self) -> None:
        """Release the socket (idempotent)."""
        if self._logged_in:
            try:
                await self._transport.send_json({"command": "logout"})
            except Exception as exc:  # noqa: BLE001 - best-effort; teardown must not raise
                logger.debug("xAPI logout failed (continuing shutdown)", error=str(exc))
        self._logged_in = False
        await self._reset_transport()
