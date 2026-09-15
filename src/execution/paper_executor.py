"""Paper (simulated) executor — safe default for testing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ..core.models import OrderResult, OrderSide, Position


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
    ) -> OrderResult:
        """Place a simulated order. Returns filled result or rejection."""

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
            result = await self._execute_buy(symbol, quantity, effective_price, order_id, now)
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
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                avg_entry_price=price,
                current_price=price,
            )

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

        # Realize *net* PnL for the closed quantity against the average entry
        # price, deducting the round-trip commission (buy side approximated with
        # the average entry, sell side at the fill price). The pipeline uses this
        # to record a true win/loss (not a fill event) — and it is now what the
        # LLM is shown, so it reflects real net economics.
        gross = (price - pos.avg_entry_price) * quantity
        buy_fee = (quantity * pos.avg_entry_price) * self.fee_pct
        realized_pnl = gross - buy_fee - sell_fee

        pos.quantity -= quantity
        if pos.quantity == 0:
            del self._positions[symbol]

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=quantity,
            price=price,
            status="filled",
            filled_at=filled_at,
            realized_pnl=realized_pnl,
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
