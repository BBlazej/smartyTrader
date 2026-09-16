"""FIFO cost-basis position tracker shared by all executors (§7.8).

Every executor — paper, Kraken, XTB — runs its fills through one tracker so
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
class SellOutcome:
    """Result of consuming lots for a sell fill."""

    gross_pnl: float = 0.0
    net_pnl: float = 0.0  # gross minus buy-side and sell-side fees (== gross when fee-free)
    closed_entries: list[ClosedEntry] = field(default_factory=list)


class PositionTracker:
    """Per-symbol FIFO cost-basis ledger fed with an executor's own fills."""

    def __init__(self) -> None:
        self._lots: dict[str, list[_Lot]] = {}

    def quantity(self, symbol: str) -> float:
        return sum(lot.quantity for lot in self._lots.get(symbol, []))

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
