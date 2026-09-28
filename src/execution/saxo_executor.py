"""Stock executor over Saxo OpenAPI (§7.66) — SIM first, live behind the §7.41 ack.

Implements the shared ``Executor`` protocol on top of :class:`SaxoClient`, mirroring
:class:`~src.execution.ccxt_executor.CcxtExecutor`'s rules:

* **Long-only, whole shares.** Positions come from the executor's own FIFO ledger
  (cost basis, entry decisions, §7.8) capped by Saxo's net position per instrument
  and marked at each cycle's close (:meth:`update_price`). A SELL closes at most what
  the ledger holds; a SELL with nothing tracked is refused — never a short. Amounts
  round *down* to ``amount_decimals`` (0 = whole shares).
* **One currency.** Cash is the chosen account's ``CashBalance``; an instrument quoted
  in a different currency is refused — the pipeline sizes in one currency, so a USD
  stock must trade from a USD account (PLAN §7.66: US stocks from a USD sub-account).
* **Market orders, venue fill prices.** Saxo reports fills in the order audit log, so
  :meth:`place_order` polls ``orderactivities`` a few times (``fill_poll_delays``):
  ``FinalFill`` books the venue's ``AveragePrice`` (sanity-checked against the request,
  §7.62); a still-working order is tracked and resolved by
  :meth:`reconcile_open_orders` on later cycles (§7.28, two-phase §7.44).
* **Symbols are mapped at the edge** (§7.59 L8): ``symbol_map`` (data → Saxo symbol,
  e.g. ``AAPL: "AAPL:xnas"``) is used only for the instrument lookup; everything above
  the executor speaks data symbols. An unmapped symbol is accepted only when the
  lookup is unambiguous — never guessed.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from ..core.models import OrderResult, OrderSide, Position
from .ccxt_executor import PendingOrderRecord, _PendingOrder
from .position_tracker import FillRecord, PositionTracker, replay_fills
from .saxo_client import SaxoClient

logger = structlog.get_logger()

_FILLED = {"finalfill"}
_DEAD = {"cancelled", "canceled", "expired", "rejected", "deleted"}


@dataclass(frozen=True)
class _Instrument:
    uic: int
    symbol: str  # Saxo symbol, e.g. "AAPL:xnas"
    currency: str | None


@dataclass(frozen=True)
class _Account:
    key: str
    client_key: str | None
    currency: str | None
    account_id: str | None


def _resolve_activity(
    entry: dict[str, Any] | None, requested: float
) -> tuple[str, float, float | None, str | None]:
    """``(status, quantity, avg_price, reason)`` from an order's latest audit entry."""
    if not entry:
        return "pending", requested, None, None
    status = str(entry.get("Status") or "").lower()
    try:
        filled = float(entry.get("FilledAmount") or 0.0)
    except (TypeError, ValueError):
        filled = 0.0
    price_raw = entry.get("AveragePrice", entry.get("ExecutionPrice"))
    try:
        price = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None
    if status in _FILLED:
        return "filled", filled or requested, price, None
    if status in _DEAD:
        if filled > 0:  # a cancel/expiry that traded is a partial fill (§7.61)
            return "filled", filled, price, "partially filled; remainder cancelled at Saxo"
        terminal = "rejected" if status == "rejected" else "cancelled"
        return terminal, requested, None, entry.get("SubStatus") or status
    return "pending", requested, None, None


class SaxoExecutor:
    """Maps the shared Executor protocol onto Saxo OpenAPI stock orders."""

    def __init__(
        self,
        client: SaxoClient,
        *,
        venue: str,
        account_key: str | None = None,
        account_currency: str | None = None,
        symbol_map: dict[str, str] | None = None,
        asset_type: str = "Stock",
        amount_decimals: int = 0,
        fill_poll_delays: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0),
        max_fill_deviation: float = 0.20,
    ) -> None:
        self._client = client
        # ``saxo-sim`` / ``saxo-live`` — stamped on this executor's rows (§7.61).
        self.venue = venue
        self._account_key = account_key
        self._account_currency = account_currency.upper() if account_currency else None
        self._symbol_map = dict(symbol_map or {})
        self._asset_type = asset_type
        self._amount_decimals = int(amount_decimals)
        self._fill_poll_delays = tuple(fill_poll_delays)
        self._max_fill_deviation = max_fill_deviation
        self._account: _Account | None = None
        self._instruments: dict[str, _Instrument] = {}
        self._tracker = PositionTracker()
        self._exit_levels: dict[str, tuple[float | None, float | None]] = {}
        self._marks: dict[str, float] = {}
        self._open_orders: dict[str, _PendingOrder] = {}
        self._closed = False

    @property
    def client(self) -> SaxoClient:
        return self._client

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001 - never abort shutdown
            logger.warning("saxo client close failed (continuing shutdown)", error=str(exc))

    # ── Account / instruments ─────────────────────────────

    async def _get_account(self) -> _Account:
        """The trading account: ``account_key`` if configured, else the only active
        account in ``account_currency`` (or the only active one). Never guessed."""
        if self._account is not None:
            return self._account
        rows = [a for a in await self._client.get_accounts() if a.get("Active", True)]
        if self._account_key:
            rows = [a for a in rows if a.get("AccountKey") == self._account_key]
        elif self._account_currency:
            rows = [a for a in rows if str(a.get("Currency", "")).upper() == self._account_currency]
        if len(rows) != 1:
            found = [f"{a.get('AccountId')} ({a.get('Currency')})" for a in rows]
            raise RuntimeError(
                "cannot choose a Saxo account: set saxo_execution.account_key "
                f"(or a unique account_currency); candidates: {found or 'none'}"
            )
        row = rows[0]
        self._account = _Account(
            key=str(row["AccountKey"]),
            client_key=row.get("ClientKey"),
            currency=str(row.get("Currency") or "").upper() or None,
            account_id=row.get("AccountId"),
        )
        logger.info(
            "saxo account selected",
            account_id=self._account.account_id,
            currency=self._account.currency,
            venue=self.venue,
        )
        return self._account

    async def _instrument(self, symbol: str) -> _Instrument:
        cached = self._instruments.get(symbol)
        if cached is not None:
            return cached
        wanted = self._symbol_map.get(symbol)
        keyword = (wanted or symbol).split(":")[0]
        rows = await self._client.find_instruments(keyword, self._asset_type)
        if wanted:
            matches = [r for r in rows if str(r.get("Symbol", "")).lower() == wanted.lower()]
        else:
            prefix = f"{symbol.lower()}:"
            matches = [
                r
                for r in rows
                if str(r.get("Symbol", "")).lower().startswith(prefix)
                or str(r.get("Symbol", "")).lower() == symbol.lower()
            ]
        if len(matches) != 1:
            listed = [r.get("Symbol") for r in (matches or rows)][:8]
            raise RuntimeError(
                f"Saxo instrument for {symbol!r} is ambiguous or unknown ({listed}); "
                f"map it in saxo_execution.symbol_map, e.g. {symbol}: '{symbol}:xnas'"
            )
        row = matches[0]
        instrument = _Instrument(
            uic=int(row["Identifier"]),
            symbol=str(row.get("Symbol")),
            currency=str(row.get("CurrencyCode") or "").upper() or None,
        )
        self._instruments[symbol] = instrument
        return instrument

    # ── Executor protocol ─────────────────────────────────

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
        def refuse(reason: str) -> OrderResult:
            logger.warning("saxo order refused", symbol=symbol, side=side.value, reason=reason)
            return OrderResult(
                order_id="", symbol=symbol, side=side, quantity=quantity, price=price,
                status="rejected", reason=reason,
            )  # fmt: skip

        if side == OrderSide.SELL:
            held = self._tracker.quantity(symbol)
            if held <= 0:
                return refuse("nothing tracked to sell (long-only: a SELL never opens a short)")
            quantity = min(quantity, held)
        amount = self._floor_amount(quantity)
        if amount <= 0:
            return refuse(f"amount {quantity:g} rounds to zero at {self._amount_decimals} decimals")

        account = await self._get_account()
        instrument = await self._instrument(symbol)
        if account.currency and instrument.currency and account.currency != instrument.currency:
            return refuse(
                f"{instrument.symbol} trades in {instrument.currency} but the account is "
                f"{account.currency} — trade it from a {instrument.currency} account"
            )

        order_id = await self._client.place_market_order(
            account_key=account.key,
            uic=instrument.uic,
            buy_sell="Buy" if side == OrderSide.BUY else "Sell",
            amount=amount,
            asset_type=self._asset_type,
        )
        status, filled_qty, fill_price, reason = "pending", amount, None, None
        for delay in self._fill_poll_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                entry = await self._client.get_order_activity(order_id)
            except Exception as exc:  # noqa: BLE001 - the reconcile loop keeps polling
                logger.warning("saxo fill poll failed", order_id=order_id, error=str(exc))
                continue
            status, filled_qty, fill_price, reason = _resolve_activity(entry, amount)
            if status != "pending":
                break

        result = OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=filled_qty,
            price=price,
            status=status,
            reason=reason,
        )
        if status == "filled":
            fill = self._checked_fill_price(order_id, fill_price, price)
            result.price = fill
            result.filled_at = datetime.now(UTC)
            if fill is not None:
                result.realized_pnl, result.closed_entries = self._record_fill(
                    symbol, side, filled_qty, fill, decision_id, stop_loss, take_profit
                )
        elif status == "pending":
            self._open_orders[order_id] = _PendingOrder(
                symbol=symbol,
                side=side,
                quantity=amount,
                decision_id=decision_id,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            logger.info("saxo order still working; reconciled next cycle", order_id=order_id)
        return result

    async def get_positions(self) -> list[Position]:
        """Long positions from the FIFO ledger, capped by Saxo's net positions."""
        net: dict[int, tuple[float, float | None]] | None = None
        try:
            net = {}
            for row in await self._client.get_net_positions():
                base = row.get("NetPositionBase") or {}
                view = row.get("NetPositionView") or {}
                if base.get("AssetType") not in (None, self._asset_type) or base.get("Uic") is None:
                    continue
                amount, current = net.get(int(base["Uic"]), (0.0, None))
                net[int(base["Uic"])] = (
                    amount + float(base.get("Amount") or 0.0),
                    view.get("CurrentPrice", current),
                )
        except Exception as exc:  # noqa: BLE001 - fall back to the ledger alone
            logger.warning("saxo net positions unreadable; ledger only", error=str(exc))
            net = None
        positions: list[Position] = []
        for symbol in self._tracker.symbols():
            qty = self._tracker.quantity(symbol)
            venue_price: float | None = None
            if net is not None:
                try:
                    uic = (await self._instrument(symbol)).uic
                except Exception as exc:  # noqa: BLE001 - uncappable → ledger alone
                    logger.warning("saxo instrument lookup failed", symbol=symbol, error=str(exc))
                else:
                    held, venue_price = net.get(uic, (0.0, None))
                    qty = min(qty, max(held, 0.0))
            if qty <= 1e-12:
                continue
            avg = self._tracker.average_price(symbol) or 0.0
            mark = self._marks.get(symbol) or (float(venue_price) if venue_price else avg)
            levels = self._exit_levels.get(symbol)
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=qty,
                    avg_entry_price=avg,
                    current_price=mark,
                    stop_loss=levels[0] if levels else None,
                    take_profit=levels[1] if levels else None,
                )
            )
        return positions

    async def get_cash(self) -> float:
        account = await self._get_account()
        balance = await self._client.get_balance(account.key, account.client_key)
        cash = balance.get("CashBalance")
        if cash is None:
            logger.warning("saxo balance has no CashBalance; reporting 0", venue=self.venue)
            return 0.0
        return float(cash)

    async def cancel_order(self, order_id: str) -> bool:
        try:
            account = await self._get_account()
            await self._client.cancel_order(order_id, account.key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("saxo cancel failed", order_id=order_id, error=str(exc))
            return False
        self._open_orders.pop(order_id, None)
        return True

    # ── Hooks: marking, reconciliation, rehydration, sleeves ─

    def update_price(self, symbol: str, new_price: float) -> None:
        if new_price > 0:
            self._marks[symbol] = new_price

    async def reconcile_open_orders(self) -> list[OrderResult]:
        """Resolve orders left working at Saxo (§7.28); two-phase like ccxt (§7.44)."""
        results: list[OrderResult] = []
        for order_id, pending in list(self._open_orders.items()):
            if pending.resolved is not None:
                results.append(pending.resolved)
                continue
            try:
                entry = await self._client.get_order_activity(order_id)
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                logger.warning("saxo order poll failed", order_id=order_id, error=str(exc))
                continue
            status, filled_qty, fill_price, reason = _resolve_activity(entry, pending.quantity)
            if status == "pending":
                continue
            result = OrderResult(
                order_id=order_id,
                symbol=pending.symbol,
                side=pending.side,
                quantity=filled_qty,
                price=fill_price,
                status=status,
                reason=reason,
            )
            if status == "filled":
                result.filled_at = datetime.now(UTC)
                if fill_price is None:
                    logger.warning("reconciled Saxo fill has no price; ledger untouched",
                                   order_id=order_id)  # fmt: skip
                else:
                    result.realized_pnl, result.closed_entries = self._record_fill(
                        pending.symbol,
                        pending.side,
                        filled_qty,
                        fill_price,
                        pending.decision_id,
                        pending.stop_loss,
                        pending.take_profit,
                    )
            pending.resolved = result
            results.append(result)
        return results

    def confirm_reconciled(self, order_id: str) -> None:
        pending = self._open_orders.get(order_id)
        if pending is not None and pending.resolved is not None:
            self._open_orders.pop(order_id, None)

    def pending_decision_id(self, order_id: str) -> int | None:
        pending = self._open_orders.get(order_id)
        return pending.decision_id if pending is not None else None

    def pending_entry_decision_ids(self, symbol: str) -> list[int | None]:
        """Decisions of BUY orders still working at the venue in ``symbol`` (§7.72 lock)."""
        return [
            p.decision_id
            for p in self._open_orders.values()
            if p.symbol == symbol and p.side == OrderSide.BUY and p.resolved is None
        ]

    def entry_decision_ids(self, symbol: str) -> list[int | None]:
        """Entry decisions of the open FIFO lots in ``symbol``, oldest first (§7.71)."""
        return self._tracker.entry_decision_ids(symbol)

    def load_fills(self, fills: list[FillRecord]) -> dict[str, int]:
        """Rebuild the ledger + exit levels from this venue's stored fills (§7.58)."""
        tracker = PositionTracker()
        replayed, levels = replay_fills(tracker, fills)
        self._tracker = tracker
        self._exit_levels = levels
        return {"replayed_fills": replayed, "open_symbols": len(tracker.symbols())}

    def working_order_sides(self, symbol: str) -> set[OrderSide]:
        """Sides with an order still working in ``symbol`` — never stacked (§7.75 b)."""
        return {
            p.side for p in self._open_orders.values() if p.symbol == symbol and p.resolved is None
        }

    def load_pending_orders(self, orders: list[PendingOrderRecord]) -> int:
        """Re-track stored ``pending`` orders so reconciliation resolves them (§7.58)."""
        for o in orders:
            if o.order_id:
                self._open_orders.setdefault(
                    o.order_id,
                    _PendingOrder(
                        symbol=o.symbol,
                        side=o.side,
                        quantity=o.quantity,
                        decision_id=o.decision_id,
                        stop_loss=o.stop_loss,
                        take_profit=o.take_profit,
                    ),
                )
        return len(self._open_orders)

    # ── Internals ─────────────────────────────────────────

    def _floor_amount(self, quantity: float) -> float:
        scale = 10**self._amount_decimals
        # A hair of tolerance so 3.0000000001 shares stays 3 (float sizing noise).
        return math.floor(quantity * scale + 1e-9) / scale

    def _checked_fill_price(
        self, order_id: str, fill: float | None, requested: float | None
    ) -> float | None:
        """Venue fill price, or the requested price when it is missing/implausible (§7.62)."""
        if fill is not None and fill > 0:
            if (
                requested
                and requested > 0
                and abs(fill - requested) / requested > self._max_fill_deviation
            ):
                logger.warning(
                    "saxo fill price far from request; booking the requested price",
                    order_id=order_id,
                    fill=fill,
                    requested=requested,
                )
                return requested
            return fill
        if requested:
            logger.warning("saxo fill has no price; booking the requested price", order_id=order_id)
        return requested

    def _record_fill(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
        decision_id: int | None,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> tuple[float | None, list[Any]]:
        if side == OrderSide.BUY:
            self._tracker.on_buy(symbol, quantity, price, decision_id=decision_id)
            self._exit_levels[symbol] = (stop_loss, take_profit)
            return None, []
        if self._tracker.quantity(symbol) <= 0:
            return None, []
        outcome = self._tracker.on_sell(symbol, quantity, price)
        if self._tracker.quantity(symbol) <= 0:
            self._exit_levels.pop(symbol, None)
        # Gross of commission, like the other venue executors (§7.8).
        return outcome.gross_pnl, list(outcome.closed_entries)
