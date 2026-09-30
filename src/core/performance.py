"""Per-sleeve performance ledger from the live record (§7.73, CHANGE.md §4.2).

Pure computation over a sleeve's own ``strategy``-tagged filled orders (§7.71) and its
``sleeve_snapshots`` equity series — the numbers the future allocator (CHANGE.md P3)
will score sleeves on, and what the dashboard's sleeve table shows today.

* **One trade = one closing fill** with its realized PnL (§7.46 — the same count the
  loss-streak tracker uses; paper PnL is net of fees, venue PnL gross, as stored).
* **Holding time** replays the sleeve's fills FIFO per symbol: each closing fill's
  hours are the quantity-weighted age of the lots it consumed.
* **Max drawdown** is peak-to-trough over the sleeve's equity snapshots.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from .timeutil import to_utc


@dataclass(frozen=True)
class SleevePerformance:
    strategy: str
    closed_trades: int
    wins: int
    losses: int
    realized_pnl: float
    win_rate: float | None
    profit_factor: float | None  # gross wins / gross losses; None without a loss
    avg_win: float | None
    avg_loss: float | None
    avg_holding_hours: float | None
    max_drawdown_pct: float | None  # fraction, from the equity snapshots

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _holding_hours(orders: list[Any]) -> list[float]:
    """FIFO-replayed holding hours of each closing fill (quantity-weighted)."""
    lots: dict[str, deque[list[Any]]] = {}
    hours: list[float] = []
    for order in orders:
        filled_at = to_utc(getattr(order, "filled_at", None))
        qty = float(order.quantity or 0.0)
        if filled_at is None or qty <= 0:
            continue
        book = lots.setdefault(order.symbol, deque())
        if order.side == "buy":
            book.append([qty, filled_at])
            continue
        remaining, weighted, consumed = qty, 0.0, 0.0
        while remaining > 1e-12 and book:
            lot = book[0]
            take = min(lot[0], remaining)
            weighted += take * (filled_at - lot[1]).total_seconds() / 3600.0
            consumed += take
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                book.popleft()
        if consumed > 0 and getattr(order, "realized_pnl", None) is not None:
            hours.append(weighted / consumed)
    return hours


def max_drawdown_fraction(values: list[float]) -> float | None:
    """Peak-to-trough drawdown of an equity series (``None`` for < 2 points)."""
    if len(values) < 2:
        return None
    peak, worst = float("-inf"), 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sleeve_performance(strategy: str, orders: list[Any], equity: list[float]) -> SleevePerformance:
    """Metrics for one sleeve from its filled orders (oldest first) + equity snapshots."""
    outcomes = [
        float(o.realized_pnl)
        for o in orders
        if o.side == "sell" and getattr(o, "realized_pnl", None) is not None
    ]
    wins = [p for p in outcomes if p > 0]
    losses = [p for p in outcomes if p < 0]
    gross_loss = -sum(losses)
    holds = _holding_hours(orders)
    return SleevePerformance(
        strategy=strategy,
        closed_trades=len(outcomes),
        wins=len(wins),
        losses=len(losses),
        realized_pnl=sum(outcomes),
        win_rate=len(wins) / len(outcomes) if outcomes else None,
        profit_factor=sum(wins) / gross_loss if gross_loss > 0 else None,
        avg_win=sum(wins) / len(wins) if wins else None,
        avg_loss=sum(losses) / len(losses) if losses else None,
        avg_holding_hours=sum(holds) / len(holds) if holds else None,
        max_drawdown_pct=max_drawdown_fraction(equity),
    )
