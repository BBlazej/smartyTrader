"""Spot order executor for any CCXT exchange (OKX Europe by default, §7.64).

Implements the shared ``Executor`` protocol (place_order, get_positions,
cancel_order, get_cash, close) on top of a CCXT exchange client. The client is
injected so this module is testable without a network connection or real API keys.

Design
------
* **Spot, long-only, always.** Positions come from the executor's own FIFO ledger of
  fills — capped by the venue's actual base-currency balance and marked at each
  cycle's close via :meth:`update_price` — so valuation, the risk gates, exit-level
  enforcement and close-all see real holdings. ``fetch_positions`` is deliberately
  never used: on venues that serve it (OKX) it reports margin/derivatives positions
  only, i.e. an empty book for a spot account (§7.64); spot long-only is a design rule.
* Cash is the free balance of the configured quote currency (``EUR`` on OKX Europe,
  where USDT is not tradable for EEA accounts under MiCA).
* **Venue order terms (§7.75).** The pipeline passes a *reference* price (the
  snapshot's last close); :class:`~src.core.config.VenueOrderSettings` turns it into a
  marketable order — a BUY limit crossed by ``entry_offset_pct``, a SELL (every spot
  close) at market by default — with the amount floored to the venue's lot size. A
  limit at the bare close rested unfilled on the first OKX demo run.
* **Fills.** OKX acknowledges ``create_order`` with an id only (no status), so one
  ``fetch_order`` right after placing resolves most fills in the same cycle; orders
  still ``open`` are re-polled every cycle via :meth:`reconcile_open_orders` (§7.28)
  and cancelled once older than ``order_ttl_seconds``. Terminal statuses update the
  stored order row and flow through the same FIFO ledger as immediate fills.
* **Fees (§7.65/§7.75).** Fees the venue reports on a fill are booked: a base-currency
  BUY fee shrinks the lot (OKX charges spot buy fees in the coin) and, with any quote
  fee, joins the lot's cost basis; SELL fees reduce the realized PnL — outcomes are
  **net** of reported commission. Lot-size dust left after a close is written off the
  ledger (never a phantom position).
* Sandbox vs live is decided by the runner (§7.41): ccxt's sandbox/demo mode when the
  exchange has one (OKX demo trading), real money only behind ``live_trading`` +
  ``LIVE_TRADING_ACK``.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import structlog

from ..core.config import VenueOrderSettings
from ..core.costs import base_currency
from ..core.models import OrderResult, OrderSide, Position
from .position_tracker import FillRecord, PositionTracker, replay_fills

logger = structlog.get_logger()

# Map CCXT order statuses to the stable statuses the rest of the system uses.
_STATUS_MAP: dict[str, str] = {
    "closed": "filled",
    "open": "pending",
    "pending": "pending",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    # ccxt's terminal "expired" (time-in-force ran out) used to fall through to
    # "pending" — the order was then re-polled forever (§7.61).
    "expired": "cancelled",
    "rejected": "rejected",
}

_PARTIAL_FILL_REASON = "partially filled; remainder cancelled at the venue"
_TTL_REASON = "unfilled after order_ttl_seconds; cancelled at the venue"

#: ccxt's unified fill-time keys, most specific first (§7.75 d). ``updated`` /
#: ``closedAt`` are kept for non-unified payloads.
_FILL_TIME_KEYS = ("lastTradeTimestamp", "lastUpdateTimestamp", "updated", "closedAt", "timestamp")


def _fee_components(raw: dict[str, Any], symbol: str) -> tuple[float, float]:
    """``(base_fee, quote_fee)`` a ccxt payload reports for a fill of *symbol*.

    Reads ccxt's ``fees`` list (falling back to a singular ``fee`` dict). Fees in
    a third currency (exchange tokens) can't be valued here and are skipped with a
    warning; unparseable payloads report no fee — booking stays gross.
    """
    fees = raw.get("fees")
    if not isinstance(fees, list) or not fees:
        single = raw.get("fee")
        fees = [single] if isinstance(single, dict) else []
    base = base_currency(symbol)
    quote = symbol.split("/")[1].upper() if "/" in symbol else ""
    base_fee = quote_fee = 0.0
    for entry in fees:
        if not isinstance(entry, dict):
            continue
        try:
            cost = float(entry.get("cost") or 0.0)
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        currency = str(entry.get("currency") or "").upper()
        if base and currency == base:
            base_fee += cost
        elif quote and currency == quote:
            quote_fee += cost
        else:
            logger.warning(
                "fill fee in a third currency not booked", symbol=symbol, currency=currency
            )
    return base_fee, quote_fee


def _resolve_status(raw: dict[str, Any], requested: float) -> tuple[str, float, str | None]:
    """Stable ``(status, quantity, reason)`` for a ccxt order payload (§7.61).

    A terminal cancel/expiry that *did* trade (``filled > 0``) is a partial fill:
    it is reported ``filled`` with the traded amount, so the ledger, the stored row
    and the restart replay all see what really changed hands — it used to be
    recorded ``cancelled`` and the filled units vanished from the books.

    A missing status is an acknowledgement, not a verdict: OKX's ``create_order``
    answers with the order id only (§7.75 f), so it maps to ``pending`` explicitly.
    """
    raw_status = raw.get("status")
    status = "pending" if raw_status is None else _STATUS_MAP.get(str(raw_status), "pending")
    try:
        filled = float(raw.get("filled") or 0.0)
    except (TypeError, ValueError):
        filled = 0.0
    if status == "filled":
        return status, filled or float(raw.get("amount") or requested), None
    if status in ("cancelled", "rejected") and filled > 0:
        return "filled", filled, _PARTIAL_FILL_REASON
    return status, float(raw.get("amount") or requested), None


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
    # When it was placed — an order older than ``order_ttl_seconds`` is cancelled (§7.75).
    placed_at: datetime | None = None


@dataclass
class PendingOrderRecord:
    """A stored ``status='pending'`` order reloaded at startup (§7.58).

    ``_open_orders`` lives in memory, so without this an order left open across a
    restart was never polled again and its row stayed ``pending`` forever.
    """

    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    decision_id: int | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    # The stored row's ``created_at`` — keeps the order TTL honest across a restart.
    placed_at: datetime | None = None


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

    async def fetch_free_balance(self, params: dict[str, Any] | None = None) -> Any:
        """Real ccxt: ``fetch_free_balance(params={})`` → ``{currency: free amount}``.

        It takes **no currency code** — passing one lands in ``params`` and crashes
        inside ccxt (found by the first OKX demo run, §7.28).
        """
        ...

    async def fetch_balance(self, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


class CcxtExecutor:
    """Maps the shared Executor protocol to CCXT spot order calls."""

    def __init__(
        self,
        client: ExchangeClient,
        *,
        quote_currency: str,
        venue: str,
        orders: VenueOrderSettings | None = None,
    ) -> None:
        self._client = client
        self._quote = quote_currency
        # Stamped on this executor's order/portfolio rows (§7.61): e.g. ``myokx-sandbox``
        # vs ``myokx-live`` — a restart replays only this venue's fills.
        self.venue = venue
        # How reference prices become venue orders, and how long they may rest (§7.75).
        self.orders = orders or VenueOrderSettings()
        self._closed = False
        # order_id -> symbol, since ccxt cancel_order needs the symbol.
        self._order_symbols: dict[str, str] = {}
        # Orders left ``open`` at the venue, re-polled each cycle (§7.28).
        self._open_orders: dict[str, _PendingOrder] = {}
        self._reconcile_unsupported_logged = False
        # Marks from each cycle's snapshot close (spot positions are valued here).
        self._marks: dict[str, float] = {}
        # Local FIFO ledger of *our* fills: the spot position book, realized_pnl on
        # closing sells, and attribution to entry decisions via closed_entries (§7.8).
        # Outcomes are net of the fees the venue reports (§7.75 e).
        self._tracker = PositionTracker()
        # Exit levels from our own entry signals (§7.9), attached to the spot
        # positions reported from the ledger (§7.41); rebuilt from storage at
        # startup by :meth:`load_fills` (§7.58).
        self._exit_levels: dict[str, tuple[float | None, float | None]] = {}
        # The venue's market metadata (lot size, minimum amount), loaded lazily.
        self._markets: dict[str, Any] | None = None

    @property
    def client(self) -> ExchangeClient:
        return self._client

    @property
    def buy_price_factor(self) -> float:
        """Worst-case BUY price relative to the reference (§7.75): sizing reserves it.

        Deliberately *not* ``slippage_pct`` — the dashboard's paper-cost overrides
        set that attribute on any executor that has it (§7.50).
        """
        return 1.0 + self.orders.entry_offset_pct

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

    # ── Venue market metadata (§7.75 c) ─────────────────

    async def _ensure_markets(self) -> dict[str, Any] | None:
        """The venue's markets (ccxt caches them); ``None`` when unavailable."""
        if self._markets:
            return self._markets
        markets = getattr(self._client, "markets", None)
        if not (isinstance(markets, dict) and markets):
            load = getattr(self._client, "load_markets", None)
            if not callable(load):
                return None
            try:
                loaded = await load()
            except Exception as exc:  # noqa: BLE001 - orders still go out unrounded
                logger.warning("load_markets failed; lot-size rounding skipped", error=str(exc))
                return None
            markets = getattr(self._client, "markets", None)
            if not (isinstance(markets, dict) and markets):
                markets = loaded
        if isinstance(markets, dict) and markets:
            self._markets = markets
        return self._markets

    def _min_amount(self, symbol: str) -> float:
        """The venue's minimum order amount for *symbol* (0 when unknown)."""
        market = (self._markets or {}).get(symbol)
        if not isinstance(market, dict):
            return 0.0
        try:
            return float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _is_dust(self, symbol: str, quantity: float) -> bool:
        """Too small to ever trade: below the venue minimum (or float noise)."""
        return quantity <= 1e-12 or quantity < self._min_amount(symbol)

    def _to_precision(self, method: str, symbol: str, value: float) -> float:
        """ccxt's ``amount_to_precision``/``price_to_precision`` (floors amounts).

        Unknown market or no helper → the value unchanged. An amount below the
        venue's precision raises in ccxt — reported as 0 (nothing tradable).
        """
        if not isinstance((self._markets or {}).get(symbol), dict):
            return value
        convert = getattr(self._client, method, None)
        if not callable(convert):
            return value
        try:
            return float(convert(symbol, value))
        except Exception:  # noqa: BLE001 - ccxt raises for sub-precision amounts
            return 0.0 if method == "amount_to_precision" else value

    def _write_off_dust(self, symbol: str) -> None:
        """Drop a ledger remainder the venue can never trade (§7.75 c).

        A close floors its amount to the lot size, so a sliver can stay behind; kept,
        it would read as an open position (max-positions, prompt, sleeve lock) that no
        exit could ever sell. The coins stay at the venue as dust.
        """
        qty = self._tracker.quantity(symbol)
        if 0 < qty and self._is_dust(symbol, qty):
            self._tracker.discard(symbol)
            self._exit_levels.pop(symbol, None)
            logger.info("ledger dust written off", symbol=symbol, quantity=qty)

    # ── Orders ─────────────────────────────────────────

    def _order_terms(
        self, symbol: str, side: OrderSide, price: float | None
    ) -> tuple[str, float | None]:
        """``(order type, limit price)`` for a reference *price* (§7.75 a)."""
        if price is None:
            return "market", None
        if side == OrderSide.SELL:
            if self.orders.exit_order_type == "market":
                return "market", None
            limit = price * (1.0 - self.orders.exit_offset_pct)
        else:
            limit = price * (1.0 + self.orders.entry_offset_pct)
        return "limit", self._to_precision("price_to_precision", symbol, limit)

    async def _confirm(self, order_id: str, symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
        """One ``fetch_order`` right after placing (§7.75 f); the ack on any failure."""
        fetch_order = getattr(self._client, "fetch_order", None)
        if not callable(fetch_order):
            return raw
        if self.orders.fill_confirm_delay_seconds > 0:
            await asyncio.sleep(self.orders.fill_confirm_delay_seconds)
        try:
            fresh = await fetch_order(order_id, symbol)
        except Exception as exc:  # noqa: BLE001 - reconciliation picks it up next cycle
            logger.warning("post-placement order poll failed", order_id=order_id, error=str(exc))
            return raw
        return fresh if isinstance(fresh, dict) and fresh else raw

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
        await self._ensure_markets()
        amount = self._to_precision("amount_to_precision", symbol, quantity)
        if amount <= 0 or self._is_dust(symbol, amount):
            logger.warning(
                "order below the venue's lot size / minimum; not sent",
                symbol=symbol,
                side=side.value,
                quantity=quantity,
                min_amount=self._min_amount(symbol),
            )
            return OrderResult(
                order_id=f"rejected-{uuid4().hex[:12]}",  # never sent; unique row key
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status="rejected",
                reason=f"quantity {quantity} below the venue minimum for {symbol}",
            )
        order_type, limit_price = self._order_terms(symbol, side, price)
        raw = await self._client.create_order(
            symbol, order_type, side.value, amount, price=limit_price
        )
        raw = raw or {}

        order_id = str(raw.get("id") or raw.get("info", {}).get("id") or "")
        self._order_symbols[order_id] = symbol
        status, filled_qty, reason = _resolve_status(raw, amount)
        if status == "pending" and order_id:
            raw = await self._confirm(order_id, symbol, raw)
            status, filled_qty, reason = _resolve_status(raw, amount)

        filled_at: datetime | None = None
        # The venue's average fill price first; else what we asked for; else the
        # reference (a market order that is still resolving).
        fill_price = raw.get("average") or raw.get("price") or limit_price or price
        if status == "filled":
            filled_at = _fill_time(raw)

        result = OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=filled_qty,
            price=float(fill_price) if fill_price is not None else None,
            status=status,
            filled_at=filled_at,
            reason=reason,
        )

        # Feed the local FIFO ledger so closing sells realize PnL back to the
        # entry decisions (§7.8). Skipped without a usable fill price.
        if status == "filled" and fill_price is not None:
            result.realized_pnl, result.closed_entries = self._record_fill(
                symbol, side, filled_qty, float(fill_price), decision_id, raw
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
                quantity=amount,
                decision_id=decision_id,
                stop_loss=stop_loss,
                take_profit=take_profit,
                placed_at=datetime.now(UTC),
            )
        return result

    def load_fills(self, fills: list[FillRecord]) -> dict[str, int]:
        """Rebuild the FIFO ledger + exit levels from stored fills (restart, §7.58).

        Called once at startup, before any cycle trades on this executor: closes
        after a restart report realized PnL + ``closed_entries`` again, spot
        positions reappear with their cost basis, and pre-restart SL/TP are
        enforced. ``fills`` must be this venue's own fills, chronological.
        """
        tracker = PositionTracker()
        replayed, levels = replay_fills(tracker, fills)
        self._tracker = tracker
        self._exit_levels = levels
        return {"replayed_fills": replayed, "open_symbols": len(tracker.symbols())}

    def load_pending_orders(self, orders: list[PendingOrderRecord]) -> int:
        """Re-track orders stored as ``pending`` so reconciliation polls them (§7.58).

        The first cycle's :meth:`reconcile_open_orders` then resolves each one
        through the normal two-phase path (ledger fed once, row patched). A row
        without a placement time starts its TTL now.
        """
        now = datetime.now(UTC)
        for o in orders:
            if not o.order_id:
                continue
            placed_at = o.placed_at
            if placed_at is not None and placed_at.tzinfo is None:
                placed_at = placed_at.replace(tzinfo=UTC)  # SQLite stores naive UTC
            self._order_symbols[o.order_id] = o.symbol
            self._open_orders.setdefault(
                o.order_id,
                _PendingOrder(
                    symbol=o.symbol,
                    side=o.side,
                    quantity=o.quantity,
                    decision_id=o.decision_id,
                    stop_loss=o.stop_loss,
                    take_profit=o.take_profit,
                    placed_at=placed_at or now,
                ),
            )
        return len(self._open_orders)

    def _record_fill(
        self,
        symbol: str,
        side: OrderSide,
        filled_qty: float,
        fill: float,
        decision_id: int | None,
        raw: dict[str, Any],
    ) -> tuple[float | None, list[Any]]:
        """Push one confirmed fill through the FIFO ledger, fees included (§7.8/§7.75 e).

        BUY: a base-currency fee shrinks the lot (OKX charges spot buy fees in the
        coin — booking the gross fill would drift the ledger above the real balance),
        and its value plus any quote fee joins the lot's cost basis. SELL: the fee
        comes off the realized PnL. Returns ``(net realized_pnl, closed_entries)``
        for closing sells.
        """
        base_fee, quote_fee = _fee_components(raw, symbol)
        if side == OrderSide.BUY:
            booked = filled_qty
            if 0 < base_fee < filled_qty:
                booked = filled_qty - base_fee
            elif base_fee >= filled_qty > 0:
                logger.warning(
                    "reported base-currency fee exceeds fill; booking gross",
                    symbol=symbol,
                    fee=base_fee,
                    filled=filled_qty,
                )
                base_fee = 0.0
            self._tracker.on_buy(
                symbol,
                booked,
                fill,
                fee=base_fee * fill + quote_fee,
                decision_id=decision_id,
            )
            return None, []
        if self._tracker.quantity(symbol) > 0:
            outcome = self._tracker.on_sell(
                symbol, filled_qty, fill, fee=quote_fee + base_fee * fill
            )
            self._write_off_dust(symbol)
            return outcome.net_pnl, list(outcome.closed_entries)
        # Nothing tracked (e.g. holdings bought outside the agent, or history
        # pruned before a restart): better no outcome than a fabricated break-even one (§7.8).
        logger.debug(
            "closing sell has no locally tracked lots; PnL not reported",
            symbol=symbol,
        )
        return None, []

    async def _cancel_stale(self, order_id: str, pending: _PendingOrder) -> dict[str, Any] | None:
        """Cancel an order past its TTL and return the venue's final word (§7.75 b).

        Re-reads the order after the cancel: it may have (partly) filled in between,
        which :func:`_resolve_status` then books. ``None`` when the venue has not
        settled it yet — the next poll retries.
        """
        logger.warning(
            "venue order past its TTL; cancelling",
            order_id=order_id,
            symbol=pending.symbol,
            side=pending.side.value,
            ttl_seconds=self.orders.order_ttl_seconds,
        )
        try:
            await self._client.cancel_order(order_id, pending.symbol)
        except Exception as exc:  # noqa: BLE001 - it may have just filled; re-read below
            logger.warning("TTL cancel failed", order_id=order_id, error=str(exc))
        try:
            raw = await self._client.fetch_order(order_id, pending.symbol)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning("order status poll failed", order_id=order_id, error=str(exc))
            return None
        return raw if isinstance(raw, dict) else None

    def _expired(self, pending: _PendingOrder, now: datetime) -> bool:
        ttl = self.orders.order_ttl_seconds
        return (
            ttl > 0
            and pending.placed_at is not None
            and (now - pending.placed_at).total_seconds() >= ttl
        )

    async def reconcile_open_orders(self) -> list[OrderResult]:
        """Re-poll orders left ``open`` at the venue and report status changes (§7.28).

        The agent calls this once per cycle. Each locally tracked pending order is
        refreshed through the client's optional ``fetch_order(id, symbol)``;
        fills flow through the same FIFO ledger (with entry-decision attribution) as
        create_order fills. An order still working past ``order_ttl_seconds`` is
        cancelled and reported ``cancelled`` (or ``filled`` for what traded, §7.75 b)
        — an exit is re-placed at the next cycle's mark. Failed polls are logged
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
        now = datetime.now(UTC)
        results: list[OrderResult] = list(redelivered)
        for order_id, pending in list(self._open_orders.items()):
            if pending.resolved is not None:
                continue  # already reported above; waiting for confirmation
            try:
                raw = await fetch_order(order_id, pending.symbol) or {}
            except Exception as exc:  # noqa: BLE001 - a failed poll is retried next cycle
                logger.warning("order status poll failed", order_id=order_id, error=str(exc))
                continue
            status, filled_qty, reason = _resolve_status(raw, pending.quantity)
            if status == "pending" and self._expired(pending, now):
                final = await self._cancel_stale(order_id, pending)
                if final is None:
                    continue
                raw = final
                status, filled_qty, reason = _resolve_status(raw, pending.quantity)
                if status == "cancelled":
                    reason = reason or _TTL_REASON
            if status == "pending":
                continue
            await self._ensure_markets()
            fill_price = raw.get("average") or raw.get("price")
            result = OrderResult(
                order_id=order_id,
                symbol=pending.symbol,
                side=pending.side,
                quantity=filled_qty,
                price=float(fill_price) if fill_price is not None else None,
                status=status,
                reason=reason,
            )
            if status == "filled":
                result.filled_at = _fill_time(raw)
                if fill_price is None:
                    logger.warning(
                        "reconciled fill reports no price; ledger left untouched",
                        order_id=order_id,
                    )
                else:
                    result.realized_pnl, result.closed_entries = self._record_fill(
                        pending.symbol,
                        pending.side,
                        filled_qty,
                        float(fill_price),
                        pending.decision_id,
                        raw,
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

    def pending_entry_decision_ids(self, symbol: str) -> list[int | None]:
        """Decisions of BUY orders still working at the venue in ``symbol`` (§7.72 lock)."""
        return [
            p.decision_id
            for p in self._open_orders.values()
            if p.symbol == symbol and p.side == OrderSide.BUY and p.resolved is None
        ]

    def working_order_sides(self, symbol: str) -> set[OrderSide]:
        """Sides with an order still working at the venue in ``symbol`` (§7.75 b).

        The pipeline never stacks a second order on the same side while one works —
        a resting exit SELL used to be re-sent every cycle.
        """
        return {
            p.side for p in self._open_orders.values() if p.symbol == symbol and p.resolved is None
        }

    def entry_decision_ids(self, symbol: str) -> list[int | None]:
        """Entry decisions of the open FIFO lots in ``symbol``, oldest first (§7.71)."""
        return self._tracker.entry_decision_ids(symbol)

    async def tradable_symbols(self) -> set[str]:
        """Active spot pairs quoted in the cash currency at *this* venue (§7.70 whitelist).

        A demo account lists far fewer pairs than the live venue whose public data
        the screener ranks (OKX EEA demo: 29 EUR spot pairs vs 243 live).
        """
        markets = await self._client.load_markets()  # type: ignore[attr-defined]
        return {
            symbol
            for symbol, market in (markets or {}).items()
            if market.get("spot")
            and market.get("active") is not False
            and str(market.get("quote", "")).upper() == self._quote.upper()
        }

    async def trading_fee(self, symbol: str) -> dict[str, float] | None:
        """This account's ``{"maker", "taker"}`` fee rates for *symbol*, if the venue says.

        The paper profile (``execution.paper_costs``) is an assumption; this is the
        account's real tier (§7.75 e — the OKX demo charged 0.20 % vs the 0.10 % profile).
        """
        fetch = getattr(self._client, "fetch_trading_fee", None)
        if not callable(fetch):
            return None
        try:
            fee = await fetch(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trading fee lookup failed", symbol=symbol, error=str(exc))
            return None
        if not isinstance(fee, dict):
            return None
        rates: dict[str, float] = {}
        for key in ("maker", "taker"):
            try:
                rates[key] = float(fee[key])
            except (KeyError, TypeError, ValueError):
                continue
        return rates or None

    def update_price(self, symbol: str, new_price: float) -> None:
        """Mark hook the pipeline calls each cycle (§7.41) — spot positions are valued here."""
        if new_price > 0:
            self._marks[symbol] = new_price

    async def get_positions(self) -> list[Position]:
        """Long positions from the FIFO ledger, capped by actual venue balances (§7.41).

        The ledger knows cost basis and entry decisions; the venue knows what is really
        held (a manual withdrawal or partial fill shrinks it). Quantity is the smaller
        of the two; a failed balance read falls back to the ledger alone. Marks come
        from :meth:`update_price` (the cycle's snapshot close), else the cost basis.
        A remainder below the venue minimum is dust, never a position (§7.75 c).
        """
        await self._ensure_markets()
        for symbol in self._tracker.symbols():
            self._write_off_dust(symbol)
        totals: dict[str, Any] | None = None
        try:
            balance = await self._client.fetch_balance()
            totals = balance.get("total") if isinstance(balance, dict) else None
        except Exception as exc:  # noqa: BLE001 - fall back to the ledger alone
            logger.warning(
                "fetch_balance failed; spot positions from the ledger only", error=str(exc)
            )
        positions: list[Position] = []
        for symbol in self._tracker.symbols():
            qty = self._tracker.quantity(symbol)
            if isinstance(totals, dict):
                held = totals.get(symbol.split("/")[0])
                if held is not None:
                    try:
                        qty = min(qty, float(held))
                    except (TypeError, ValueError):
                        pass
            if self._is_dust(symbol, qty):
                continue
            avg = self._tracker.average_price(symbol) or 0.0
            levels = self._exit_levels.get(symbol)
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=qty,
                    avg_entry_price=avg,
                    current_price=self._marks.get(symbol, avg),
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
        # No currency argument: ccxt's signature is (params={}) — §7.28 smoke-run find.
        balance = await self._client.fetch_free_balance()
        return _extract_quote_balance(balance, self._quote)


def _extract_quote_balance(balance: Any, quote: str) -> float:
    """Pull the quote-currency free balance out of a ccxt ``fetch_free_balance`` payload.

    Real CCXT returns a dict keyed by currency code whose values are nested
    ``{"free": x, "used": y, "total": z}`` dicts (venues list every funded
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


def _fill_time(raw: dict[str, Any]) -> datetime:
    """When a filled order traded: ccxt's unified timestamps, else now (§7.75 d)."""
    for key in _FILL_TIME_KEYS:
        parsed = _parse_ms_timestamp(raw.get(key))
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def create_ccxt_executor(
    client: ExchangeClient,
    *,
    quote_currency: str,
    venue: str,
    orders: VenueOrderSettings | None = None,
) -> CcxtExecutor:
    """Wrap an existing CCXT client in a :class:`CcxtExecutor`."""
    return CcxtExecutor(client, quote_currency=quote_currency, venue=venue, orders=orders)
