"""Paper (simulated) executor — safe default for testing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ..core.models import OrderResult, OrderSide, Position
from .position_tracker import FillRecord, PositionTracker


class PaperExecutor:
    """Simulates order execution with an in-memory portfolio.

    Orders are filled instantly at the requested price (or a configurable slippage).
    Positions and cash balance are tracked deterministically — no external calls.
    """

    def __init__(
        self,
        initial_cash: float = 100_000.0,
        slippage_pct: float = 0.001,
        fee_pct: float = 0.0,
    ) -> None:
        self.initial_cash = initial_cash
        self._cash = initial_cash
        self.slippage_pct = slippage_pct
        # Per-side commission as a fraction of notional (e.g. 0.0026 = 0.26%).
        # Deducted from cash on both buys and sells and from realized PnL, so
        # paper PnL is net-of-fee and comparable to the "win rate after fees" gate.
        # 0.0 (default) disables fees — used by tests that assert exact cash flow.
        self.fee_pct = fee_pct
        # symbol → Position
        self._positions: dict[str, Position] = {}
        # FIFO cost-basis ledger over our own fills. It mirrors _positions but
        # keeps per-lot basis + the entry decision id, so closing sells report
        # closed_entries for the "learn from your track record" backfill (§7.8).
        self._tracker = PositionTracker()
        # order_id → OrderResult (for cancellation lookup)
        self._orders: dict[str, OrderResult] = {}

    @property
    def cash(self) -> float:
        return self._cash

    async def get_cash(self) -> float:
        """Return current cash balance. Part of the Executor Protocol."""
        return self._cash

    async def close(self) -> None:
        """Part of the Executor Protocol.

        The paper executor holds only in-memory state and no persistent
        connections, so there is nothing to release — this is a no-op.
        """
        return

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
        """Place a simulated order. Returns filled result or rejection.

        ``stop_loss``/``take_profit`` are attached to the opened position so the
        pipeline can enforce them on later cycles (§7.9); ignored on sells."""

        # Market orders — use current market price from position if available
        effective_price: float
        if price is not None:
            effective_price = price
        elif symbol in self._positions:
            effective_price = self._positions[symbol].current_price
        else:
            return OrderResult(
                order_id=_gen_order_id(),
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="rejected",
                reason=f"Cannot fill market {side.value} without price for {symbol}",
            )

        # Apply slippage
        if side == OrderSide.BUY:
            effective_price *= 1 + self.slippage_pct
        else:
            effective_price *= 1 - self.slippage_pct

        now = datetime.now(UTC)
        order_id = _gen_order_id()

        if side == OrderSide.BUY:
            result = await self._execute_buy(
                symbol,
                quantity,
                effective_price,
                order_id,
                now,
                decision_id,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        else:
            result = await self._execute_sell(symbol, quantity, effective_price, order_id, now)

        self._orders[order_id] = result
        return result

    async def _execute_buy(
        self,
        symbol: str,
        quantity: float,
        price: float,
        order_id: str,
        filled_at: datetime,
        decision_id: int | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        cost = quantity * price
        fee = cost * self.fee_pct
        total = cost + fee

        if total > self._cash:
            return OrderResult(
                order_id=order_id,
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=quantity,
                price=price,
                status="rejected",
                reason=f"Insufficient cash: need {total:.2f}, have {self._cash:.2f}",
            )

        self._cash -= total

        if symbol in self._positions:
            pos = self._positions[symbol]
            total_qty = pos.quantity + quantity
            new_avg = (pos.avg_entry_price * pos.quantity + price * quantity) / total_qty
            pos.quantity = total_qty
            pos.avg_entry_price = new_avg
            # Latest plan wins when adding to a position (§7.9).
            if stop_loss is not None:
                pos.stop_loss = stop_loss
            if take_profit is not None:
                pos.take_profit = take_profit
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                avg_entry_price=price,
                current_price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

        self._tracker.on_buy(symbol, quantity, price, fee=fee, decision_id=decision_id)

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            price=price,
            status="filled",
            filled_at=filled_at,
        )

    async def _execute_sell(
        self,
        symbol: str,
        quantity: float,
        price: float,
        order_id: str,
        filled_at: datetime,
    ) -> OrderResult:
        pos = self._positions.get(symbol)
        if pos is None or pos.quantity < quantity:
            available = pos.quantity if pos else 0.0
            return OrderResult(
                order_id=order_id,
                symbol=symbol,
                side=OrderSide.SELL,
                quantity=quantity,
                price=price,
                status="rejected",
                reason=f"Insufficient position: need {quantity}, have {available}",
            )

        proceeds = quantity * price
        sell_fee = proceeds * self.fee_pct
        self._cash += proceeds - sell_fee

        # Realize *net* PnL via the shared FIFO tracker: each consumed lot
        # carries its own entry price and paid commission, so the result equals
        # the old average-cost calculation for single-lot books (§7.8) while also
        # attributing PnL back to the originating decisions (closed_entries).
        outcome = self._tracker.on_sell(symbol, quantity, price, fee=sell_fee)

        pos.quantity -= quantity
        if pos.quantity <= 1e-12:
            del self._positions[symbol]

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=quantity,
            price=price,
            status="filled",
            filled_at=filled_at,
            realized_pnl=outcome.net_pnl,
            closed_entries=outcome.closed_entries,
        )

    async def get_positions(self) -> list[Position]:
        """Return current positions.

        ``current_price`` reflects the most recent mark applied by the decision
        pipeline (each cycle re-marks open positions to the snapshot's last
        close via :meth:`update_price`); prices do not move on their own
        between cycles.
        """
        return list(self._positions.values())

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Paper orders fill instantly, so only unfilled can be cancelled."""
        order = self._orders.get(order_id)
        if order is None or order.status != "pending":
            return False
        order.status = "cancelled"
        return True

    def load_portfolio_state(
        self,
        cash: float,
        positions: list[Position],
        fills: list[FillRecord] | None = None,
    ) -> dict[str, int]:
        """Replace the book wholesale from persisted state (restart rehydration [§7.7]).

        Called once at startup by the runner — before any cycle has traded on this
        executor — so decisions/orders persisted in earlier runs reference trades
        that still exist, and the risk trackers can be seeded from real values.

        ``fills`` (optional, chronological order) rebuilds the FIFO lot ledger
        from historical filled orders, so positions opened before a restart keep
        their per-lot cost basis **and entry decision ids** — closing sells after
        the restart still report ``closed_entries`` for the "learn from your track
        record" backfill (§7.25). Stored orders carry no commission, so rebuilt
        lots are fee-free; any quantity gap between the replayed fills and the
        loaded positions (pruned order history) is topped up with one synthetic
        lot per position at its ``avg_entry_price``, keeping tracker and book
        consistent. Returns counts for logging: ``replayed_fills`` / ``synthetic_lots``.
        """
        self._cash = cash
        self._positions = {p.symbol: p for p in positions}

        tracker = PositionTracker()
        replayed = 0
        for f in fills or []:
            if f.side == "buy":
                tracker.on_buy(f.symbol, f.quantity, f.price, decision_id=f.decision_id)
            elif f.side == "sell":
                # Outcome PnL of historical sells was already backfilled into the
                # decisions then; we only need the ledger state after them.
                tracker.on_sell(f.symbol, f.quantity, f.price)
            else:  # pragma: no cover - guard against bad rows
                continue
            replayed += 1

        synthetic = 0
        for p in positions:
            missing = p.quantity - tracker.quantity(p.symbol)
            if missing > 1e-12:
                tracker.on_buy(p.symbol, missing, p.avg_entry_price)
                synthetic += 1

        self._tracker = tracker
        return {"replayed_fills": replayed, "synthetic_lots": synthetic}

    def update_price(self, symbol: str, new_price: float) -> None:
        """Re-mark an open position at the latest market price.

        Called by :class:`DecisionPipeline` every cycle with the snapshot's
        last close, so paper positions are never valued at a frozen entry
        price (keeps unrealized PnL, portfolio snapshots and the daily-loss
        rule market-honest). No-op for symbols with no open position.
        """
        if symbol in self._positions:
            self._positions[symbol].current_price = new_price


def _gen_order_id() -> str:
    return f"paper-{uuid.uuid4().hex[:12]}"
