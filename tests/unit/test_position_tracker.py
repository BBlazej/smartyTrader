"""Unit tests for the shared FIFO PositionTracker (§7.8).

The tracker is pure in-memory math: every executor feeds it fills, and closing
sells get realized PnL plus per-entry-decision attribution back from it.
"""

from __future__ import annotations

import pytest

from src.execution.position_tracker import PositionTracker


class TestBuys:
    def test_empty_quantity_is_zero(self) -> None:
        tracker = PositionTracker()
        assert tracker.quantity("BTC/USDT") == 0.0

    def test_on_buy_adds_lot(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 2.0, 100.0, decision_id=1)
        assert tracker.quantity("BTC/USDT") == pytest.approx(2.0)

    def test_nonpositive_quantity_ignored(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 0.0, 100.0)
        tracker.on_buy("BTC/USDT", -5.0, 100.0)
        assert tracker.quantity("BTC/USDT") == 0.0


class TestFifoConsumption:
    def test_single_lot_full_close(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 1.0, 100.0, decision_id=7)
        outcome = tracker.on_sell("BTC/USDT", 1.0, 120.0)

        assert outcome.gross_pnl == pytest.approx(20.0)
        assert outcome.net_pnl == pytest.approx(20.0)
        assert len(outcome.closed_entries) == 1
        assert outcome.closed_entries[0].entry_decision_id == 7
        assert outcome.closed_entries[0].pnl == pytest.approx(20.0)
        assert tracker.quantity("BTC/USDT") == 0.0

    def test_partial_consumption_keeps_remainder(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 4.0, 100.0, decision_id=1)
        outcome = tracker.on_sell("BTC/USDT", 1.0, 110.0)

        assert outcome.gross_pnl == pytest.approx(10.0)
        assert tracker.quantity("BTC/USDT") == pytest.approx(3.0)

    def test_fifo_order_across_lots(self) -> None:
        # First lot is profitable, second is a loser; FIFO must consume front first.
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 1.0, 100.0, decision_id=1)
        tracker.on_buy("BTC/USDT", 1.0, 200.0, decision_id=2)

        outcome = tracker.on_sell("BTC/USDT", 1.0, 150.0)
        assert outcome.gross_pnl == pytest.approx(50.0)  # vs the *first* lot's basis
        assert [e.entry_decision_id for e in outcome.closed_entries] == [1]
        assert tracker.quantity("BTC/USDT") == pytest.approx(1.0)

    def test_multi_lot_sell_aggregates_per_entry(self) -> None:
        # Two lots from the SAME decision: their nets must aggregate into one entry.
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 1.0, 100.0, decision_id=5)
        tracker.on_buy("BTC/USDT", 1.0, 110.0, decision_id=5)

        outcome = tracker.on_sell("BTC/USDT", 2.0, 120.0)
        assert len(outcome.closed_entries) == 1
        assert outcome.closed_entries[0].entry_decision_id == 5
        assert outcome.closed_entries[0].pnl == pytest.approx(20.0 + 10.0)

    def test_multi_lot_sell_keeps_entries_distinct(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 1.0, 100.0, decision_id=1)
        tracker.on_buy("BTC/USDT", 1.0, 110.0, decision_id=2)

        outcome = tracker.on_sell("BTC/USDT", 2.0, 120.0)
        by_decision = {e.entry_decision_id: e.pnl for e in outcome.closed_entries}
        assert by_decision == {1: pytest.approx(20.0), 2: pytest.approx(10.0)}

    def test_dust_lot_is_popped(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 1.0, 100.0, decision_id=1)
        tracker.on_sell("BTC/USDT", 1.0 - 1e-13, 100.0)
        # The residue below the dust threshold must not linger as an open lot.
        assert tracker.quantity("BTC/USDT") == pytest.approx(0.0)


class TestFees:
    def test_net_pnl_subtracts_both_sides_of_commission(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 1.0, 100.0, fee=1.0, decision_id=1)
        outcome = tracker.on_sell("BTC/USDT", 1.0, 120.0, fee=0.5)

        assert outcome.gross_pnl == pytest.approx(20.0)
        assert outcome.net_pnl == pytest.approx(20.0 - 1.0 - 0.5)

    def test_sell_fee_split_pro_rata_across_lots(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 1.0, 100.0, decision_id=1)
        tracker.on_buy("BTC/USDT", 3.0, 100.0, decision_id=2)

        # Selling 2 of 4 units: each consumed lot bears fee * (take / fill qty).
        outcome = tracker.on_sell("BTC/USDT", 2.0, 100.0, fee=4.0)
        by_decision = {e.entry_decision_id: e.pnl for e in outcome.closed_entries}
        assert by_decision[1] == pytest.approx(-4.0 * (1.0 / 2.0))
        assert by_decision[2] == pytest.approx(-4.0 * (1.0 / 2.0))

    def test_buy_fee_split_pro_rata_on_partial_close(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy("BTC/USDT", 4.0, 100.0, fee=8.0, decision_id=1)
        outcome = tracker.on_sell("BTC/USDT", 1.0, 100.0)
        # Only a quarter of the lot's paid commission is realized on this slice.
        assert outcome.net_pnl == pytest.approx(-2.0)
