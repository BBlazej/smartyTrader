"""Kraken (testnet) order executor via CCXT.

Implements the shared ``Executor`` protocol (place_order, get_positions,
cancel_order, get_cash, close) on top of a CCXT exchange client. The client is
injected so this module is testable without a network connection or real API keys.

Known venue limitations (§7.6)
------------------------------
* Kraken **spot** via CCXT does not serve ``fetch_positions`` (raises
  ``NotSupported``); :meth:`KrakenExecutor.get_positions` degrades gracefully to
  an empty list (warned once), so the max-open-positions gate and position
  valuation see only cash on the keyed path until fills are tracked locally.
* The pipeline submits marketable *limit* orders (priced at the snapshot's last
  close); those usually come back ``closed`` in the ``create_order`` payload,
  which is now recorded with fill price (``average``) and ``filled_at``. Orders
  left ``open`` are reported ``pending`` — per-cycle status reconciliation is
  not implemented yet.
* A live-keyed smoke test against the Kraken testnet still needs a
  network-enabled environment (the dev sandbox blocks outbound HTTPS).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
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
    ) -> (
        Any
    ): ...  # real ccxt returns a currency→{free,total} dict; simplified stubs may return a float

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
        # One-time warning guard for the Kraken-spot fetch_positions gap (§7.6).
        self._positions_unsupported_logged = False

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

        # Real ccxt reports fills with an ``average`` price and millisecond
        # timestamps; a marketable limit (what the pipeline sends) usually comes
        # back ``closed`` in the create_order payload itself. Record both so
        # "filled" orders carry when/where they filled instead of a bare status.
        filled_at: datetime | None = None
        fill_price = raw.get("average") or raw.get("price") or price
        if status == "filled":
            filled_at = _parse_ms_timestamp(raw.get("updated") or raw.get("closedAt")) or (
                _parse_ms_timestamp(raw.get("timestamp")) or datetime.now(UTC)
            )

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=float(raw.get("filled") or raw.get("amount") or quantity),
            price=float(fill_price) if fill_price is not None else None,
            status=status,
            filled_at=filled_at,
        )

    async def get_positions(self) -> list[Position]:
        try:
            raw_positions = await self._client.fetch_positions()
        except Exception as exc:  # noqa: BLE001
            # Kraken *spot* via CCXT does not serve fetch_positions (NotSupported),
            # so the max-open-positions gate and portfolio valuation see nothing
            # on the keyed path. Degrade gracefully — one warning, empty list —
            # instead of crashing every cycle (§7.6). See the module docstring.
            if not self._positions_unsupported_logged:
                self._positions_unsupported_logged = True
                logger.warning(
                    "fetch_positions failed on the keyed venue; position-based "
                    "gates will see no positions (Kraken spot does not support this call)",
                    error=str(exc),
                )
            return []
        positions: list[Position] = []
        for pos in raw_positions or []:
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
        return _extract_quote_balance(balance, self._quote)


def _extract_quote_balance(balance: Any, quote: str) -> float:
    """Pull the quote-currency free balance out of a ccxt ``fetch_free_balance`` payload.

    Real CCXT returns a dict keyed by currency code whose values are nested
    ``{"free": x, "used": y, "total": z}`` dicts (Kraken includes every funded
    currency); simplified clients/tests may return a bare float. A missing
    quote key means the account holds none of it → 0.0.
    """
    if balance is None:
        return 0.0
    if isinstance(balance, (int, float)):
        return float(balance)
    if not isinstance(balance, dict):
        logger.warning("unexpected fetch_free_balance payload type", type=type(balance).__name__)
        return 0.0

    entry: Any = balance.get(quote)
    if entry is None:
        # Case-insensitive fallback — ccxt casing can differ per venue.
        quote_upper = quote.upper()
        for key, value in balance.items():
            if isinstance(key, str) and key.upper() == quote_upper:
                entry = value
                break
    if entry is None:  # quote currency absent → nothing of it held
        return 0.0
    if isinstance(entry, (int, float)):
        return float(entry)
    if isinstance(entry, dict):
        free = entry.get("free", entry.get("total", 0.0))
        try:
            return float(free)
        except (TypeError, ValueError):
            logger.warning("unparseable quote balance entry", quote=quote, entry=entry)
            return 0.0
    return 0.0


def _parse_ms_timestamp(value: Any) -> datetime | None:
    """Convert a ccxt millisecond timestamp into an aware datetime (or ``None``)."""
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def create_kraken_executor(client: ExchangeClient, quote_currency: str = "USDT") -> KrakenExecutor:
    """Wrap an existing CCXT client in a KrakenExecutor."""
    return KrakenExecutor(client, quote_currency=quote_currency)
