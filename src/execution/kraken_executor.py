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
  left ``open`` are remembered locally and re-polled every cycle via
  :meth:`KrakenExecutor.reconcile_open_orders` (§7.28): terminal statuses update
  the stored order row and flow through the same FIFO ledger as create_order fills.
* A live-keyed smoke test against the Kraken testnet still needs a
  network-enabled environment (the dev sandbox blocks outbound HTTPS).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from ..core.models import OrderResult, OrderSide, Position, PositionSide
from .position_tracker import PositionTracker

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


@dataclass
class _PendingOrder:
    """A venue order left ``open``, remembered locally for per-cycle reconciliation (§7.28)."""

    symbol: str
    side: OrderSide
    quantity: float
    decision_id: int | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    # §7.44: the terminal result once the venue resolved the order. It is re-delivered
    # on every reconcile until the agent confirms it persisted the transition, so a
    # DB error can never lose it (and the ledger is only ever fed once).
    resolved: OrderResult | None = None


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
        # Orders left ``open`` at the venue, re-polled each cycle (§7.28).
        self._open_orders: dict[str, _PendingOrder] = {}
        self._reconcile_unsupported_logged = False
        # One-time warning guard for the Kraken-spot fetch_positions gap (§7.6).
        self._positions_unsupported_logged = False
        # Local FIFO ledger of *our* fills. Kraken spot gives no fetch_positions,
        # so this both enables realized_pnl on closing sells and attributes it to
        # entry decisions via closed_entries (§7.8). Venue fees are not in the
        # create_order payload, so tracked PnL is gross of commission.
        self._tracker = PositionTracker()
        # Exit levels from our own entry signals (§7.9). Kraken spot reports no
        # positions at all, so this map only matters once position visibility
        # lands; kept for parity with the other executors.
        self._exit_levels: dict[str, tuple[float | None, float | None]] = {}

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
        decision_id: int | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
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

        filled_qty = float(raw.get("filled") or raw.get("amount") or quantity)
        result = OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=filled_qty,
            price=float(fill_price) if fill_price is not None else None,
            status=status,
            filled_at=filled_at,
        )

        # Feed the local FIFO ledger so closing sells realize PnL back to the
        # entry decisions (§7.8). Skipped without a usable fill price.
        if status == "filled" and fill_price is not None:
            fill = float(fill_price)
            result.realized_pnl, result.closed_entries = self._record_fill(
                symbol, side, filled_qty, fill, decision_id
            )
            if side == OrderSide.BUY:
                # Remember the plan; these are *not* venue-side stop orders —
                # enforcement is the pipeline's per-cycle check (§7.9).
                self._exit_levels[symbol] = (stop_loss, take_profit)
            if self._tracker.quantity(symbol) <= 0:
                self._exit_levels.pop(symbol, None)
        elif status == "pending" and order_id:
            # Left open at the venue: reconcile_open_orders re-polls it each cycle.
            self._open_orders[order_id] = _PendingOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                decision_id=decision_id,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        return result

    def _record_fill(
        self,
        symbol: str,
        side: OrderSide,
        filled_qty: float,
        fill: float,
        decision_id: int | None,
    ) -> tuple[float | None, list[Any]]:
        """Push one confirmed fill through the FIFO ledger (§7.8).

        Returns ``(realized_pnl, closed_entries)`` for closing sells — gross of
        venue commission (create_order/fetch_order payloads report none); the
        paper executor's net-of-fee counterpart tracks its own fees.
        """
        if side == OrderSide.BUY:
            self._tracker.on_buy(symbol, filled_qty, fill, decision_id=decision_id)
            return None, []
        if self._tracker.quantity(symbol) > 0:
            outcome = self._tracker.on_sell(symbol, filled_qty, fill)
            return outcome.gross_pnl, list(outcome.closed_entries)
        # Nothing tracked (e.g. holdings opened before a restart): better
        # no outcome than a fabricated break-even one (§7.8).
        logger.debug(
            "closing sell has no locally tracked lots; PnL not reported",
            symbol=symbol,
        )
        return None, []

    async def reconcile_open_orders(self) -> list[OrderResult]:
        """Re-poll orders left ``open`` at the venue and report status changes (§7.28).

        The agent calls this once per cycle. Each locally tracked pending order is
        refreshed through the client's optional ``fetch_order(id, symbol)``;
        fills flow through the same FIFO ledger (with entry-decision attribution) as
        create_order fills. Still-open orders stay tracked; failed polls are logged
        fail-soft and retried next cycle.

        **Two-phase (§7.44):** a terminal status (filled / cancelled / rejected) is
        cached and re-delivered on every call until :meth:`confirm_reconciled` says
        the agent persisted it — a storage error then delays the record instead of
        losing it. The ledger is fed exactly once, when the status first resolves.
        """
        if not self._open_orders:
            return []
        redelivered = [p.resolved for p in self._open_orders.values() if p.resolved is not None]
        if len(redelivered) == len(self._open_orders):
            return redelivered
        fetch_order = getattr(self._client, "fetch_order", None)
        if not callable(fetch_order):
            if not self._reconcile_unsupported_logged:
                self._reconcile_unsupported_logged = True
                logger.warning(
                    "cannot reconcile pending orders: venue client provides no fetch_order"
                )
            return []
        results: list[OrderResult] = list(redelivered)
        for order_id, pending in list(self._open_orders.items()):
            if pending.resolved is not None:
                continue  # already reported above; waiting for confirmation
            try:
                raw = await fetch_order(order_id, pending.symbol) or {}
            except Exception as exc:  # noqa: BLE001 - a failed poll is retried next cycle
                logger.warning("order status poll failed", order_id=order_id, error=str(exc))
                continue
            status = _STATUS_MAP.get(str(raw.get("status", "open")), "pending")
            if status == "pending":
                continue
            fill_price = raw.get("average") or raw.get("price")
            default_qty = pending.quantity if status == "filled" else 0.0
            filled_qty = float(raw.get("filled") or raw.get("amount") or default_qty)
            result = OrderResult(
                order_id=order_id,
                symbol=pending.symbol,
                side=pending.side,
                quantity=filled_qty,
                price=float(fill_price) if fill_price is not None else None,
                status=status,
            )
            if status == "filled":
                result.filled_at = _parse_ms_timestamp(raw.get("updated") or raw.get("closedAt"))
                if fill_price is None:
                    logger.warning(
                        "reconciled fill reports no price; ledger left untouched",
                        order_id=order_id,
                    )
                else:
                    fill = float(fill_price)
                    result.realized_pnl, result.closed_entries = self._record_fill(
                        pending.symbol, pending.side, filled_qty, fill, pending.decision_id
                    )
                    if pending.side == OrderSide.BUY:
                        # The entry plan was captured when the order was placed;
                        # enforcement stays local (§7.9).
                        self._exit_levels[pending.symbol] = (pending.stop_loss, pending.take_profit)
            pending.resolved = result
            results.append(result)
        return results

    def confirm_reconciled(self, order_id: str) -> None:
        """The agent persisted this order's terminal status — stop re-delivering it (§7.44)."""
        pending = self._open_orders.get(order_id)
        if pending is not None and pending.resolved is not None:
            self._open_orders.pop(order_id, None)

    def pending_decision_id(self, order_id: str) -> int | None:
        """Entry decision of a tracked venue order (lets the agent re-create a lost row)."""
        pending = self._open_orders.get(order_id)
        return pending.decision_id if pending is not None else None

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
            raw_qty = float(pos.get("contracts") or pos.get("amount") or 0.0)
            if raw_qty == 0.0:
                continue
            # §7.38 (find #4): ccxt encodes shorts either as negative contracts
            # (net-position payloads) or via the ``side`` field (detail payloads).
            # They map honestly to side=SHORT with an absolute quantity — never a
            # positive-quantity long in disguise.
            raw_side = str(pos.get("side") or "").lower()
            side = PositionSide.SHORT if raw_side == "short" or raw_qty < 0 else PositionSide.LONG
            avg_entry = float(pos.get("entryPrice") or pos.get("averageCost") or 0.0)
            current = float(pos.get("markPrice") or pos.get("entryPrice") or avg_entry)
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
        symbol = self._order_symbols.get(order_id)
        if symbol is None:
            logger.warning("cannot cancel unknown order", order_id=order_id)
            return False
        try:
            await self._client.cancel_order(order_id, symbol)
            # Cancelled by us — stop reconciling; the venue would only echo back
            # what we already know (§7.28).
            self._open_orders.pop(order_id, None)
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
