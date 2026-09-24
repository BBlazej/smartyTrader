"""XTB (demo) order executor via xAPI.

Implements the shared ``Executor`` protocol (``place_order``, ``get_positions``,
``cancel_order``, ``get_cash``) on top of an injected :class:`XTBClient`. The client
is injected so this module is testable without a network connection or an approved
XTB demo account.

Wiring status (§7.16)
---------------------
The real client has landed: :class:`src.execution.xtb_client.XApiClient` speaks the
xAPI WebSocket protocol (``wss://ws.xapi.pro/{demo,real}``, classic ``login`` auth
with the account id + xAPI verification code — there is **no OAuth2 endpoint**).
The stocks runner wires this executor only when ``xtb_execution.enabled`` AND both
``XTB_ACCOUNT_ID``/``XTB_ACCOUNT_PASSWORD`` are set; anything missing keeps the
paper executor (still the safe default), and the block is deliberately outside the
dashboard's safe-config whitelist.

Order semantics — reduce first, never flip (§7.40)
--------------------------------------------------
xAPI is position-based: ``cmd=SELL, type=OPEN`` *opens a short*, it does not close a
long. So :meth:`XTBExecutor.place_order` first looks at the symbol's open trades: an
order opposite to open trades **closes** them (FIFO, ``type=CLOSE`` via
:meth:`XTBClient.close_trade`, partially if needed) and never flips into a new
opposite position with any remainder. With nothing to close, a BUY opens a long;
a SELL is refused unless ``allow_short`` is set — this is a long-only agent.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol

import structlog

from ..core.models import ClosedEntry, OrderResult, OrderSide, Position, PositionSide
from .position_tracker import PositionTracker

logger = structlog.get_logger()

# Map xAPI order statuses to the stable statuses the rest of the system uses.
_STATUS_MAP: dict[str, str] = {
    "filled": "filled",
    "pending": "pending",
    "open": "pending",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


class XTBClient(Protocol):
    """Minimal async xAPI surface the executor depends on.

    Implemented for real by :class:`src.execution.xtb_client.XApiClient` (§7.16);
    tests pass a mock satisfying this protocol — zero network, per project rules.
    """

    async def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float | None = None,
    ) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    async def get_positions(self) -> list[dict[str, Any]]: ...

    async def get_balance(self) -> float: ...

    async def get_open_trades(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    async def close_trade(
        self,
        order: int,
        symbol: str,
        cmd: int,
        volume: float,
        price: float | None = None,
    ) -> dict[str, Any]: ...


_OPENED_LONG = 0  # xAPI cmd of a BUY-opened (long) trade
_OPENED_SHORT = 1  # xAPI cmd of a SELL-opened (short) trade


class XTBExecutor:
    """Maps the shared Executor protocol to XTB order calls via xAPI."""

    def __init__(self, client: XTBClient, *, allow_short: bool = False) -> None:
        self._client = client
        self._closed = False
        # §7.40: this agent is long-only; a SELL with nothing to close is refused
        # instead of silently opening a short on the venue.
        self._allow_short = allow_short
        # Local FIFO ledger of our own fills (§7.8): realized_pnl on closing
        # sells + attribution back to entry decisions via closed_entries.
        # xAPI's create_order reports no commission, so tracked PnL is gross.
        self._tracker = PositionTracker()
        # Exit levels from our entry signals, re-attached to positions the venue
        # reports (xAPI payloads don't carry them) so §7.9 can enforce them.
        self._exit_levels: dict[str, tuple[float | None, float | None]] = {}

    @property
    def client(self) -> XTBClient:
        return self._client

    async def close(self) -> None:
        """Release the underlying xAPI client (e.g. its HTTP session).

        A real async xAPI client may hold a persistent session; closing it
        keeps shutdown clean. Idempotent and fail-soft (a test stand-in without
        ``close`` is a no-op; a failing close never aborts shutdown).
        """
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("xtb client close failed (continuing shutdown)", error=str(exc))

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
        decision_id: int | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        # §7.40: reduce first — an order opposite to open trades closes them.
        closing_cmd = _OPENED_LONG if side == OrderSide.SELL else _OPENED_SHORT
        opposite = [
            t for t in await self._client.get_open_trades(symbol) if t.get("cmd") == closing_cmd
        ]
        if opposite:
            return await self._close_trades(symbol, side, quantity, price, opposite)
        if side == OrderSide.SELL and not self._allow_short:
            logger.warning("refusing SELL with no open long (would open a short)", symbol=symbol)
            return OrderResult(
                order_id="",
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status="rejected",
                reason=(
                    f"No open long in {symbol} to close — an xAPI SELL would open a short "
                    "(long-only agent)"
                ),
            )

        raw = await self._client.create_order(symbol, side.value, quantity, price=price)
        raw = raw or {}

        order_id = str(raw.get("order_id") or raw.get("id") or "")
        status = _STATUS_MAP.get(str(raw.get("status", "pending")), "pending")
        filled_qty = float(raw.get("quantity", quantity))

        result = OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=filled_qty,
            price=float(price) if price is not None else None,
            status=status,
            filled_at=None,
        )

        # xAPI fills the requested price; track it locally so closing sells
        # realize PnL back to their entry decisions (§7.8).
        if status == "filled" and price is not None:
            if side == OrderSide.BUY:
                self._tracker.on_buy(symbol, filled_qty, float(price), decision_id=decision_id)
                # Local levels only — enforcement is the pipeline's per-cycle
                # check, not a venue-side stop order (§7.9).
                self._exit_levels[symbol] = (stop_loss, take_profit)
            elif self._tracker.quantity(symbol) > 0:
                outcome = self._tracker.on_sell(symbol, filled_qty, float(price))
                result.realized_pnl = outcome.gross_pnl
                result.closed_entries = outcome.closed_entries
            else:
                # Holdings opened before a restart leave no local lots; report no
                # outcome rather than a fabricated break-even one (§7.8).
                logger.debug(
                    "closing sell has no locally tracked lots; PnL not reported",
                    symbol=symbol,
                )
            if self._tracker.quantity(symbol) <= 0:
                self._exit_levels.pop(symbol, None)
        return result

    async def _close_trades(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None,
        trades: list[dict[str, Any]],
    ) -> OrderResult:
        """Close open trades FIFO up to ``quantity`` with ``type=CLOSE`` (§7.40).

        Any remainder beyond the open volume is dropped (never flipped into a new
        opposite position). Filled volume flows through the FIFO ledger — ``on_sell``
        for closed longs, ``cover`` for closed shorts — for realized PnL + entry
        attribution (gross of commission, booked at the requested price, find #8).
        """
        remaining = quantity
        closed_volume = 0.0
        order_ids: list[str] = []
        fill_price = price
        failure: str | None = None
        for trade in trades:
            if remaining <= 1e-12:
                break
            volume = min(float(trade.get("volume", 0.0)), remaining)
            if volume <= 0:
                continue
            raw = (
                await self._client.close_trade(
                    int(trade["order"]), symbol, int(trade["cmd"]), volume, price=price
                )
                or {}
            )
            status = _STATUS_MAP.get(str(raw.get("status", "pending")), "pending")
            if status != "filled":
                failure = f"close of trade {trade['order']} came back {status}"
                break
            order_ids.append(str(raw.get("order_id") or ""))
            if raw.get("price") is not None:
                fill_price = float(raw["price"])
            closed_volume += volume
            remaining -= volume

        if remaining > 1e-12 and failure is None:
            logger.warning(
                "close quantity exceeds open volume; remainder dropped (no flip)",
                symbol=symbol,
                requested=quantity,
                closed=closed_volume,
            )

        realized: float | None = None
        entries: list[ClosedEntry] = []
        if closed_volume > 0 and fill_price is not None:
            if side == OrderSide.SELL and self._tracker.quantity(symbol) > 0:
                outcome = self._tracker.on_sell(symbol, closed_volume, fill_price)
                realized, entries = outcome.gross_pnl, outcome.closed_entries
            elif side == OrderSide.BUY and self._tracker.short_quantity(symbol) > 0:
                outcome = self._tracker.cover(symbol, closed_volume, fill_price)
                realized, entries = outcome.gross_pnl, outcome.closed_entries
        if self._tracker.quantity(symbol) <= 0 and self._tracker.short_quantity(symbol) <= 0:
            self._exit_levels.pop(symbol, None)

        return OrderResult(
            order_id=order_ids[0] if order_ids else "",
            symbol=symbol,
            side=side,
            quantity=closed_volume if closed_volume > 0 else quantity,
            price=fill_price,
            status="filled" if closed_volume > 0 else "rejected",
            reason=failure,
            realized_pnl=realized,
            closed_entries=entries,
        )

    async def get_positions(self) -> list[Position]:
        raw_positions = await self._client.get_positions()
        positions: list[Position] = []
        for pos in raw_positions or []:
            raw_qty = float(pos.get("quantity") or pos.get("contracts") or 0.0)
            if raw_qty == 0.0:
                continue
            # xAPI volumes are positive with direction in ``side`` (§7.38);
            # signed payloads are honored too. Shorts never masquerade as longs.
            side = (
                PositionSide.SHORT
                if str(pos.get("side", "long")).lower() == "short" or raw_qty < 0
                else PositionSide.LONG
            )
            avg_entry = float(pos.get("avg_entry_price") or pos.get("entry_price") or 0.0)
            current = float(pos.get("current_price") or pos.get("mark_price") or avg_entry)
            levels = self._exit_levels.get(str(pos.get("symbol")))
            positions.append(
                Position(
                    symbol=str(pos.get("symbol")),
                    quantity=abs(raw_qty),
                    avg_entry_price=avg_entry,
                    current_price=current,
                    side=side,
                    stop_loss=levels[0] if levels else None,
                    take_profit=levels[1] if levels else None,
                )
            )
        return positions

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._client.cancel_order(order_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel failed", order_id=order_id, error=str(exc))
            return False

    async def get_cash(self) -> float:
        balance = await self._client.get_balance()
        return float(balance)
