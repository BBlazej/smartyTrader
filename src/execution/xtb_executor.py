"""XTB (demo) order executor via xAPI.

Implements the shared ``Executor`` protocol (``place_order``, ``get_positions``,
``cancel_order``, ``get_cash``) on top of an injected :class:`XTBClient`. The client
is injected so this module is testable without a network connection or an approved
XTB demo account.

External blocker
----------------
XTB's xAPI requires an **approved demo account** plus an **OAuth2 flow** to obtain a
token (see ``PLAN.md``). :class:`XTBClient` is that seam: a real xAPI client implements
its three methods (``create_order``, ``cancel_order``, ``get_positions``,
``get_balance``) and is passed to :class:`XTBExecutor`. Until that lands, the paper
executor (``paper_executor.py``) remains the safe default.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol

import structlog

from ..core.models import OrderResult, OrderSide, Position

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

    A real xAPI client (OAuth2 + REST) implements these four methods; tests pass a
    mock that satisfies this protocol.
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
    ) -> OrderResult:
        raw = await self._client.create_order(symbol, side.value, quantity, price=price)
        raw = raw or {}

        order_id = str(raw.get("order_id") or raw.get("id") or "")
        status = _STATUS_MAP.get(str(raw.get("status", "pending")), "pending")

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=float(raw.get("quantity", quantity)),
            price=float(price) if price is not None else None,
            status=status,
            filled_at=None,
        )

    async def get_positions(self) -> list[Position]:
        raw_positions = await self._client.get_positions()
        positions: list[Position] = []
        for pos in raw_positions or []:
            quantity = float(pos.get("quantity") or pos.get("contracts") or 0.0)
            if quantity <= 0:
                continue
            avg_entry = float(pos.get("avg_entry_price") or pos.get("entry_price") or 0.0)
            current = float(pos.get("current_price") or pos.get("mark_price") or avg_entry)
            positions.append(
                Position(
                    symbol=str(pos.get("symbol")),
                    quantity=quantity,
                    avg_entry_price=avg_entry,
                    current_price=current,
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
