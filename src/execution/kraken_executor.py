"""Kraken (testnet) order executor via CCXT.

Implements the shared ``Executor`` protocol (place_order, get_positions,
cancel_order, get_cash, close) on top of a CCXT exchange client. The client is
injected so this module is testable without a network connection or real API keys.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol

import structlog

from ..core.models import OrderResult, OrderSide, Position

logger = structlog.get_logger()

# Map CCXT order statuses to the stable statuses the rest of the system uses.
_STATUS_MAP: dict[str, str] = {
    "closed": "filled",
    "open": "pending",
    "pending": "pending",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "rejected": "rejected",
}


class ExchangeClient(Protocol):
    """Minimal async CCXT surface the executor depends on."""

    async def create_order(
        self,
        symbol: str,
        type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel_order(self, id: str, symbol: str) -> dict[str, Any]: ...

    async def fetch_free_balance(
        self, code: str | None = None, params: dict[str, Any] | None = None
    ) -> float: ...

    async def fetch_positions(
        self, symbols: list[str] | None = None, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


class KrakenExecutor:
    """Maps the shared Executor protocol to Kraken order calls via CCXT."""

    def __init__(self, client: ExchangeClient, quote_currency: str = "USDT") -> None:
        self._client = client
        self._quote = quote_currency
        self._closed = False
        # order_id -> symbol, since ccxt cancel_order needs the symbol.
        self._order_symbols: dict[str, str] = {}

    @property
    def client(self) -> ExchangeClient:
        return self._client

    async def close(self) -> None:
        """Release the underlying exchange, closing its ``aiohttp`` session.

        The keyed order client is a CCXT exchange that owns a lazily-created
        ``aiohttp.ClientSession``; without an explicit ``close()`` it leaks on
        shutdown. Idempotent and fail-soft (a test stand-in without ``close``
        is a no-op; a failing close never aborts shutdown).
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
            logger.warning("exchange close failed (continuing shutdown)", error=str(exc))

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> OrderResult:
        order_type = "limit" if price is not None else "market"
        raw = await self._client.create_order(symbol, order_type, side.value, quantity, price=price)
        raw = raw or {}

        order_id = str(raw.get("id") or raw.get("info", {}).get("id") or "")
        self._order_symbols[order_id] = symbol
        status = _STATUS_MAP.get(str(raw.get("status", "open")), "pending")

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=float(raw.get("amount", quantity)),
            price=float(price) if price is not None else None,
            status=status,
            filled_at=None,
        )

    async def get_positions(self) -> list[Position]:
        raw_positions = await self._client.fetch_positions()
        positions: list[Position] = []
        for pos in raw_positions or []:
            if not pos.get("side"):
                continue
            quantity = float(pos.get("contracts") or pos.get("amount") or 0.0)
            if quantity <= 0:
                continue
            avg_entry = float(pos.get("entryPrice") or pos.get("averageCost") or 0.0)
            current = float(pos.get("markPrice") or pos.get("entryPrice") or avg_entry)
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
        symbol = self._order_symbols.get(order_id)
        if symbol is None:
            logger.warning("cannot cancel unknown order", order_id=order_id)
            return False
        try:
            await self._client.cancel_order(order_id, symbol)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel failed", order_id=order_id, error=str(exc))
            return False

    async def get_cash(self) -> float:
        balance = await self._client.fetch_free_balance(self._quote)
        return float(balance)


def create_kraken_executor(client: ExchangeClient, quote_currency: str = "USDT") -> KrakenExecutor:
    """Wrap an existing CCXT client in a KrakenExecutor."""
    return KrakenExecutor(client, quote_currency=quote_currency)
