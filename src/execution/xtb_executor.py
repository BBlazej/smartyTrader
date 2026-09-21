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
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol

import structlog

from ..core.models import OrderResult, OrderSide, Position
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


class XTBExecutor:
    """Maps the shared Executor protocol to XTB order calls via xAPI."""

    def __init__(self, client: XTBClient) -> None:
        self._client = client
        self._closed = False
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

    async def get_positions(self) -> list[Position]:
        raw_positions = await self._client.get_positions()
        positions: list[Position] = []
        for pos in raw_positions or []:
            quantity = float(pos.get("quantity") or pos.get("contracts") or 0.0)
            if quantity <= 0:
                continue
            avg_entry = float(pos.get("avg_entry_price") or pos.get("entry_price") or 0.0)
            current = float(pos.get("current_price") or pos.get("mark_price") or avg_entry)
            levels = self._exit_levels.get(str(pos.get("symbol")))
            positions.append(
                Position(
                    symbol=str(pos.get("symbol")),
                    quantity=quantity,
                    avg_entry_price=avg_entry,
                    current_price=current,
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
