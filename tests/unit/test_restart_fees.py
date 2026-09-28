"""Restart replay books fills net of the venue's fees, like the live path (§7.77).

Found by the agent-driven OKX demo round trip: OKX took the BUY fee in BTC, so the
account received less than the order filled. The live ledger knew that (§7.75), but
the order row stored only the gross fill, so after a restart the replayed ledger
overstated the coins. The SELL then sold 1.26e-5 BTC of the demo account's
pre-loaded coins, and its PnL omitted the buy fee (−1.16 EUR reported vs ≈ −2.08 EUR
real). Fees are now persisted on the order row and replayed through the same
:func:`book_fill` rule.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.base_agent import _fee_fields
from src.agents.crypto_agent import CryptoAgent
from src.core.config import RiskSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import OHLCV, Action, MarketSnapshot, OrderResult, OrderSide, TradeSignal
from src.core.rehydration import rehydrate_venue_executor
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.position_tracker import FillRecord, PositionTracker, replay_fills
from tests.unit.test_venue_orders import BUY_FEE_RATE, SELL_FEE_RATE, SYMBOL, FakeVenue, _executor

DEMO = "myokx-sandbox"
PRICE = 100_000.0


@pytest.fixture()
async def storage(tmp_db_path: str) -> Storage:
    store = Storage(tmp_db_path, agent="crypto")
    await store.initialize()
    store.bind_venue(DEMO)
    yield store
    await store.close()


def _agent(storage: Storage, executor, signal: TradeSignal) -> CryptoAgent:
    candles = [OHLCV(open=PRICE, high=PRICE, low=PRICE, close=PRICE, volume=1.0)] * 3
    provider = MagicMock()
    provider.fetch_snapshot = AsyncMock(
        side_effect=lambda symbol, timeframe: MarketSnapshot(
            symbol=symbol, timeframe=timeframe, candles=candles
        )
    )
    llm = AsyncMock()
    llm.ask_trade_signal = AsyncMock(side_effect=lambda **_: signal.model_copy())
    llm.last_metrics = None
    pipeline = DecisionPipeline(
        provider=provider,
        llm_client=llm,
        risk_engine=RiskEngine(RiskSettings(max_position_pct=0.01)),
        executor=executor,
        storage=storage,
    )
    return CryptoAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=pipeline.risk_engine,
        llm_client=llm,
        pairs=[SYMBOL],
    )


def _buy() -> TradeSignal:
    return TradeSignal(
        symbol=SYMBOL, action=Action.BUY, confidence=0.9, reasoning="in", stop_loss=90_000.0
    )


class TestFeesArePersisted:
    async def test_agent_stores_the_fees_of_an_immediate_fill(self, storage: Storage) -> None:
        venue = FakeVenue()
        await _agent(storage, _executor(venue), _buy()).run_cycle()
        (row,) = await storage.get_filled_orders()
        assert row.fee_base == pytest.approx(row.quantity * BUY_FEE_RATE)
        assert row.fee_quote is None

    async def test_reconciled_fill_patches_the_fees_onto_the_row(self, storage: Storage) -> None:
        venue = FakeVenue(rest=frozenset({"buy"}))
        executor = _executor(venue)
        agent = _agent(storage, executor, _buy())
        await agent.run_cycle()
        (pending,) = await storage.get_pending_orders()
        assert pending.fee_base is None

        venue.orders[pending.order_id]["state"] = "fill"
        await agent._reconcile_orders()
        (row,) = await storage.get_filled_orders()
        assert row.fee_base == pytest.approx(row.quantity * BUY_FEE_RATE)

    def test_fee_fields_only_for_filled_orders_and_present_values(self) -> None:
        filled = OrderResult(
            order_id="1",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            quantity=1.0,
            status="filled",
            fee_base=0.001,
        )
        assert _fee_fields(filled) == {"fee_base": 0.001}
        pending = filled.model_copy(update={"status": "pending"})
        assert _fee_fields(pending) == {}

    async def test_a_later_status_patch_never_blanks_stored_fees(self, storage: Storage) -> None:
        await storage.save_order("F-1", SYMBOL, "buy", 1.0, PRICE, "filled", fee_base=0.001)
        await storage.update_order_status("F-1", "filled", price=PRICE)
        (row,) = await storage.get_filled_orders()
        assert row.fee_base == pytest.approx(0.001)


class TestRestartReplayMatchesTheLiveLedger:
    async def test_no_oversell_and_net_pnl_after_a_restart(self, storage: Storage) -> None:
        venue = FakeVenue()
        venue.base = 1.0  # the OKX demo account's pre-loaded BTC — the balance cap can't help
        live = _executor(venue)
        buy = await live.place_order(SYMBOL, OrderSide.BUY, 0.001, price=PRICE, decision_id=7)
        await storage.save_order(
            buy.order_id,
            SYMBOL,
            "buy",
            buy.quantity,
            buy.price,
            "filled",
            decision_id=7,
            filled_at=buy.filled_at,
            **_fee_fields(buy),
        )
        live_qty = live._tracker.quantity(SYMBOL)
        live_avg = live._tracker.average_price(SYMBOL)

        restarted = _executor(venue)
        await rehydrate_venue_executor(restarted, storage)
        (position,) = await restarted.get_positions()
        assert position.quantity == pytest.approx(live_qty)  # net of the BTC fee, not gross
        assert restarted._tracker.average_price(SYMBOL) == pytest.approx(live_avg)

        sell = await restarted.place_order(SYMBOL, OrderSide.SELL, position.quantity, price=PRICE)
        assert venue.base == pytest.approx(1.0)  # none of the pre-loaded coins were sold
        # The outcome is the cash that actually moved: both fees included.
        assert sell.realized_pnl == pytest.approx(venue.cash - 10_000.0, rel=1e-9)
        paid = buy.quantity * buy.price
        received = position.quantity * PRICE * (1 - SELL_FEE_RATE)
        assert sell.realized_pnl == pytest.approx(received - paid, rel=1e-9)
        assert sell.closed_entries[0].entry_decision_id == 7

    def test_replay_books_exactly_like_the_live_fill(self) -> None:
        replayed = PositionTracker()
        replay_fills(
            replayed,
            [
                FillRecord(SYMBOL, "buy", 1.0, 100.0, decision_id=1, fee_base=0.01, fee_quote=0.5),
                FillRecord(SYMBOL, "sell", 0.5, 110.0, fee_quote=0.2),
            ],
        )
        live = PositionTracker()
        from src.execution.position_tracker import book_fill

        book_fill(live, SYMBOL, "buy", 1.0, 100.0, fee_base=0.01, fee_quote=0.5, decision_id=1)
        book_fill(live, SYMBOL, "sell", 0.5, 110.0, fee_quote=0.2)
        assert replayed.quantity(SYMBOL) == pytest.approx(live.quantity(SYMBOL))
        assert replayed.quantity(SYMBOL) == pytest.approx(0.49)
        assert replayed.average_price(SYMBOL) == pytest.approx(live.average_price(SYMBOL))
        # The remaining lot carries only its share of the buy fees (1.0 + 0.5 EUR).
        rest = replayed.on_sell(SYMBOL, 0.49, 100.0)
        assert rest.net_pnl == pytest.approx(-1.5 * 0.49 / 0.99)


class TestPartialSellFees:
    """A partial sell takes its share of the buy fee *with* it (found with §7.77)."""

    def test_buy_fee_is_charged_once_across_partial_sells(self) -> None:
        tracker = PositionTracker()
        tracker.on_buy(SYMBOL, 1.0, 100.0, fee=2.0)
        first = tracker.on_sell(SYMBOL, 0.25, 100.0)
        second = tracker.on_sell(SYMBOL, 0.75, 100.0)
        assert first.net_pnl == pytest.approx(-0.5)
        assert second.net_pnl == pytest.approx(-1.5)
        assert first.net_pnl + second.net_pnl == pytest.approx(-2.0)  # not -3.5

    def test_short_cover_fee_is_charged_once_too(self) -> None:
        tracker = PositionTracker()
        tracker.open_short(SYMBOL, 1.0, 100.0, fee=2.0)
        first = tracker.cover(SYMBOL, 0.5, 100.0)
        second = tracker.cover(SYMBOL, 0.5, 100.0)
        assert first.net_pnl + second.net_pnl == pytest.approx(-2.0)

    def test_legacy_rows_without_fees_replay_as_before(self) -> None:
        tracker = PositionTracker()
        replay_fills(tracker, [FillRecord(SYMBOL, "buy", 1.0, 100.0)])
        assert tracker.quantity(SYMBOL) == pytest.approx(1.0)


class TestMigration:
    async def test_fee_columns_are_added_to_an_old_orders_table(self, tmp_db_path: str) -> None:
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, symbol TEXT, "
            "side TEXT, quantity FLOAT, price FLOAT, status TEXT, decision_id INTEGER, "
            "filled_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO orders (order_id, symbol, side, quantity, price, status) "
            "VALUES ('old', 'BTC/EUR', 'buy', 1.0, 100.0, 'filled')"
        )
        conn.commit()
        conn.close()

        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            (row,) = await storage.get_recent_orders()
            assert row.fee_base is None and row.fee_quote is None
        finally:
            await storage.close()
