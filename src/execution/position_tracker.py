"""FIFO cost-basis position tracker shared by all executors (§7.8).

Every executor — paper, ccxt spot, XTB — runs its fills through one tracker so
``OrderResult.realized_pnl`` is populated on *closing* fills everywhere (real
venues used to leave it ``None``, silently skipping the "learn from your track
record" backfill). FIFO lots also carry the decision that opened each lot, so a
closing sell can attribute its PnL back to the originating **entry** decisions
(``closed_entries``), not just to its own sell row.

Pure in-memory computation — no I/O, no venue knowledge. Cash movements stay in
the executor; this only tracks cost basis and realized outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import ClosedEntry


@dataclass
class _Lot:
    """One open buy lot (FIFO order: consumed front-to-back)."""

    quantity: float
    price: float
    fee_paid: float = 0.0  # commission paid to open this whole lot
    decision_id: int | None = None  # the entry decision that created it


@dataclass
class FillRecord:
    """A historical fill replayed into a tracker at startup (§7.25).

    Deliberately fee-free: stored orders do not record commissions, so rebuilt
    lots carry ``fee_paid=0`` — the cost basis and decision attribution are what
    matter for post-restart outcome tracking.
    """

    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    price: float
    decision_id: int | None = None
    # The entry decision's SL/TP (buys only) — venue executors re-arm local exit
    # levels from these after a restart (§7.58).
    stop_loss: float | None = None
    take_profit: float | None = None


ExitLevels = tuple[float | None, float | None]


@dataclass
class SellOutcome:
    """Result of consuming lots for a sell fill."""

    gross_pnl: float = 0.0
    net_pnl: float = 0.0  # gross minus buy-side and sell-side fees (== gross when fee-free)
    closed_entries: list[ClosedEntry] = field(default_factory=list)


class PositionTracker:
    """Per-symbol FIFO cost-basis ledger fed with an executor's own fills.

    Two ledgers per symbol (§7.38, find #9): the long book (``on_buy``/``on_sell``,
    what every spot executor uses today) and an explicit short book
    (``open_short``/``cover``) for margin/derivatives adapters. The sides are
    deliberately *not* inferred from buy/sell verbs — a spot executor's sell can
    never accidentally open a short; direction is chosen by the caller.
    """

    def __init__(self) -> None:
        self._lots: dict[str, list[_Lot]] = {}
        self._short_lots: dict[str, list[_Lot]] = {}

    def quantity(self, symbol: str) -> float:
        return sum(lot.quantity for lot in self._lots.get(symbol, []))

    def symbols(self) -> list[str]:
        """Symbols with an open long ledger (§7.41 spot position view)."""
        return [s for s, lots in self._lots.items() if lots]

    def average_price(self, symbol: str) -> float | None:
        """Quantity-weighted cost of the open long lots (``None`` when flat)."""
        lots = self._lots.get(symbol, [])
        qty = sum(lot.quantity for lot in lots)
        if qty <= 0:
            return None
        return sum(lot.quantity * lot.price for lot in lots) / qty

    def on_buy(
        self,
        symbol: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        decision_id: int | None = None,
    ) -> None:
        """Record a buy fill as a new FIFO lot."""
        if quantity <= 0:
            return
        lots = self._lots.setdefault(symbol, [])
        lots.append(_Lot(quantity=quantity, price=price, fee_paid=fee, decision_id=decision_id))

    def on_sell(self, symbol: str, quantity: float, price: float, fee: float = 0.0) -> SellOutcome:
        """Consume ``quantity`` FIFO at ``price``; returns the realized outcome.

        ``fee`` is the sell-side commission for this whole fill; it is split
        pro-rata across the consumed lots so each entry decision is attributed
        its share of net PnL. Assumes enough quantity is held (the executor
        validates before calling).
        """
        outcome = SellOutcome()
        remaining = quantity
        lots = self._lots.get(symbol, [])
        per_entry: dict[int | None, float] = {}

        while remaining > 0 and lots:
            lot = lots[0]
            take = min(lot.quantity, remaining)
            gross = (price - lot.price) * take
            buy_fee_share = lot.fee_paid * (take / lot.quantity) if lot.quantity > 0 else 0.0
            sell_fee_share = fee * (take / quantity) if quantity > 0 else 0.0
            net = gross - buy_fee_share - sell_fee_share

            outcome.gross_pnl += gross
            outcome.net_pnl += net
            per_entry[lot.decision_id] = per_entry.get(lot.decision_id, 0.0) + net

            lot.quantity -= take
            remaining -= take
            if lot.quantity <= 1e-12:
                lots.pop(0)

        if not lots:
            self._lots.pop(symbol, None)

        outcome.closed_entries = [
            ClosedEntry(entry_decision_id=decision_id, pnl=pnl)
            for decision_id, pnl in per_entry.items()
        ]
        return outcome

    # ── Short side (§7.38) ─────────────────────────────

    def short_quantity(self, symbol: str) -> float:
        return sum(lot.quantity for lot in self._short_lots.get(symbol, []))

    def open_short(
        self,
        symbol: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        decision_id: int | None = None,
    ) -> None:
        """Record a short-opening sell fill as a new FIFO short lot (§7.38)."""
        if quantity <= 0:
            return
        lots = self._short_lots.setdefault(symbol, [])
        lots.append(_Lot(quantity=quantity, price=price, fee_paid=fee, decision_id=decision_id))

    def cover(self, symbol: str, quantity: float, price: float, fee: float = 0.0) -> SellOutcome:
        """Consume ``quantity`` short lots FIFO at the covering buy ``price``.

        Short PnL is inverted: profit when the cover price sits *below* the
        entry. Attribution works exactly like :meth:`on_sell` — each consumed
        lot carries its opening ``decision_id``, and fees split pro-rata.
        """
        outcome = SellOutcome()
        remaining = quantity
        lots = self._short_lots.get(symbol, [])
        per_entry: dict[int | None, float] = {}

        while remaining > 0 and lots:
            lot = lots[0]
            take = min(lot.quantity, remaining)
            gross = (lot.price - price) * take  # short: entry minus cover
            open_fee_share = lot.fee_paid * (take / lot.quantity) if lot.quantity > 0 else 0.0
            cover_fee_share = fee * (take / quantity) if quantity > 0 else 0.0
            net = gross - open_fee_share - cover_fee_share

            outcome.gross_pnl += gross
            outcome.net_pnl += net
            per_entry[lot.decision_id] = per_entry.get(lot.decision_id, 0.0) + net

            lot.quantity -= take
            remaining -= take
            if lot.quantity <= 1e-12:
                lots.pop(0)

        if not lots:
            self._short_lots.pop(symbol, None)

        outcome.closed_entries = [
            ClosedEntry(entry_decision_id=decision_id, pnl=pnl)
            for decision_id, pnl in per_entry.items()
        ]
        return outcome


def replay_fills(
    tracker: PositionTracker, fills: list[FillRecord]
) -> tuple[int, dict[str, ExitLevels]]:
    """Replay chronological historical fills into ``tracker`` (§7.25 / §7.58).

    Long book only, like every executor's live path: a sell consumes lots FIFO
    (its historical outcome was already backfilled then — only the ledger state
    after it matters). Returns ``(replayed, exit_levels)`` where ``exit_levels``
    mirrors the live rule — the latest buy's SL/TP per symbol, dropped once the
    symbol is flat.
    """
    replayed = 0
    levels: dict[str, ExitLevels] = {}
    for f in fills:
        if f.side == "buy":
            tracker.on_buy(f.symbol, f.quantity, f.price, decision_id=f.decision_id)
            levels[f.symbol] = (f.stop_loss, f.take_profit)
        elif f.side == "sell":
            tracker.on_sell(f.symbol, f.quantity, f.price)
        else:  # guard against bad rows
            continue
        replayed += 1
        if tracker.quantity(f.symbol) <= 1e-12:
            levels.pop(f.symbol, None)
    return replayed, levels
