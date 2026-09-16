"""Tests for the SQLite storage layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.core.storage import Storage


@pytest.fixture()
async def storage(tmp_db_path: str) -> Storage:
    s = Storage(tmp_db_path)
    await s.initialize()
    yield s
    await s.close()


class TestMarketSnapshots:
    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, storage: Storage) -> None:
        candles = json.dumps([{"o": 60000, "h": 61000, "l": 59500, "c": 60500, "v": 100}])

        await storage.save_market_snapshot(
            symbol="BTC/USDT",
            timeframe="1h",
            candles_json=candles,
            indicators_json=json.dumps({"rsi": 45.2}),
        )

        rows = await storage.get_recent_snapshots("BTC/USDT")
        assert len(rows) == 1
        assert rows[0].symbol == "BTC/USDT"
        assert rows[0].timeframe == "1h"
        assert json.loads(rows[0].indicators_json)["rsi"] == pytest.approx(45.2)

    @pytest.mark.asyncio
    async def test_get_recent_limits_results(self, storage: Storage) -> None:
        for i in range(5):
            await storage.save_market_snapshot(
                symbol="ETH/USDT",
                timeframe="1h",
                candles_json=json.dumps([{"o": 3000 + i}]),
            )

        rows = await storage.get_recent_snapshots("ETH/USDT", limit=2)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_empty_for_unknown_symbol(self, storage: Storage) -> None:
        rows = await storage.get_recent_snapshots("UNKNOWN")
        assert len(rows) == 0


class TestLLMDecisions:
    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, storage: Storage) -> None:
        decision_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.85,
            reasoning="Bullish divergence",
            stop_loss=60000.0,
            take_profit=70000.0,
            risk_verdict="approved",
            risk_reason=None,
        )

        assert decision_id > 0

        rows = await storage.get_recent_decisions("BTC/USDT")
        assert len(rows) == 1
        assert rows[0].action == "buy"
        assert rows[0].confidence == pytest.approx(0.85)
        assert rows[0].risk_verdict == "approved"

    @pytest.mark.asyncio
    async def test_get_recent_without_symbol(self, storage: Storage) -> None:
        await storage.save_llm_decision(
            "BTC/USDT", "buy", 0.9, "test", 60000, 70000, "approved", None
        )
        await storage.save_llm_decision(
            "ETH/USDT", "sell", 0.8, "test", 2900, 3100, "approved", None
        )

        rows = await storage.get_recent_decisions(limit=5)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_rejected_decision_stored(self, storage: Storage) -> None:
        await storage.save_llm_decision(
            symbol="AAPL",
            action="buy",
            confidence=0.3,
            reasoning="Weak signal",
            stop_loss=None,
            take_profit=None,
            risk_verdict="rejected",
            risk_reason="Confidence too low",
        )

        rows = await storage.get_recent_decisions("AAPL")
        assert len(rows) == 1
        assert rows[0].risk_verdict == "rejected"
        assert rows[0].risk_reason == "Confidence too low"

    @pytest.mark.asyncio
    async def test_realized_pnl_defaults_to_none(self, storage: Storage) -> None:
        # A decision with no closed trade yet has no outcome — must be NULL, not 0.
        decision_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="momentum",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )

        rows = await storage.get_recent_decisions("BTC/USDT")
        assert rows[0].id == decision_id
        assert rows[0].realized_pnl is None

    @pytest.mark.asyncio
    async def test_set_realized_pnl_updates_row(self, storage: Storage) -> None:
        decision_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="sell",
            confidence=0.7,
            reasoning="take profit",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )

        await storage.set_realized_pnl(decision_id, -12.5)

        rows = await storage.get_recent_decisions("BTC/USDT")
        assert rows[0].realized_pnl == pytest.approx(-12.5)

    @pytest.mark.asyncio
    async def test_set_realized_pnl_fail_soft(self, storage: Storage) -> None:
        # A DB error while stamping the outcome must never break a trading cycle.
        from unittest.mock import AsyncMock, patch

        with patch.object(Storage, "_session", new=AsyncMock(side_effect=RuntimeError("db down"))):
            await storage.set_realized_pnl(1, 10.0)  # must not raise


class TestDecisionAttribution:
    """Fallback marking and entry-decision PnL accumulation (§7.8)."""

    @pytest.mark.asyncio
    async def test_is_fallback_persisted_and_excluded_from_recent(self, storage: Storage) -> None:
        real_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="real decision",
            stop_loss=1.0,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        fallback_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="hold",
            confidence=0.0,
            reasoning="LLM unavailable — safe fallback",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
            is_fallback=True,
        )

        rows = await storage.get_recent_decisions("BTC/USDT")
        ids = {row.id for row in rows}
        assert real_id in ids
        assert fallback_id not in ids  # audit-only, never re-fed into prompts

    @pytest.mark.asyncio
    async def test_default_is_not_fallback(self, storage: Storage) -> None:
        decision_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="hold",
            confidence=0.7,
            reasoning="plain hold",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        rows = await storage.get_recent_decisions("BTC/USDT")
        assert [row.id for row in rows] == [decision_id]

    @pytest.mark.asyncio
    async def test_add_realized_pnl_accumulates_from_null(self, storage: Storage) -> None:
        decision_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="entry",
            stop_loss=1.0,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        assert (await storage.get_recent_decisions("BTC/USDT"))[0].realized_pnl is None

        await storage.add_realized_pnl(decision_id, 5.0)
        await storage.add_realized_pnl(decision_id, -2.0)  # a later tranche at a loss

        rows = {r.id: r for r in await storage.get_recent_decisions("BTC/USDT")}
        assert rows[decision_id].realized_pnl == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_add_realized_pnl_fail_soft(self, storage: Storage) -> None:
        # A missing row must warn and continue, never raise into the cycle.
        await storage.add_realized_pnl(99_999, 10.0)


class TestOrders:
    @pytest.mark.asyncio
    async def test_save_order(self, storage: Storage) -> None:
        order_id = await storage.save_order(
            order_id="order-abc-123",
            symbol="BTC/USDT",
            side="buy",
            quantity=0.5,
            price=60000.0,
            status="filled",
            decision_id=1,
            filled_at=datetime.now(UTC),
        )

        assert order_id > 0


class TestPortfolioSnapshots:
    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, storage: Storage) -> None:
        positions = json.dumps([{"symbol": "AAPL", "qty": 10}])

        await storage.save_portfolio_snapshot(
            cash=10000.0,
            positions_json=positions,
            total_value=11500.0,
            unrealized_pnl=500.0,
        )

        snapshot = await storage.get_latest_portfolio_snapshot()
        assert snapshot is not None
        assert snapshot.cash == pytest.approx(10000.0)
        assert snapshot.total_value == pytest.approx(11500.0)
        assert snapshot.unrealized_pnl == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_latest_returns_most_recent(self, storage: Storage) -> None:
        await storage.save_portfolio_snapshot(
            cash=10000.0, positions_json="[]", total_value=10000.0
        )
        await storage.save_portfolio_snapshot(cash=9500.0, positions_json="[]", total_value=9500.0)

        snapshot = await storage.get_latest_portfolio_snapshot()
        assert snapshot is not None
        assert snapshot.cash == pytest.approx(9500.0)

    @pytest.mark.asyncio
    async def test_empty_returns_none(self, storage: Storage) -> None:
        snapshot = await storage.get_latest_portfolio_snapshot()
        assert snapshot is None

    @pytest.mark.asyncio
    async def test_history_ordered_descending(self, storage: Storage) -> None:
        for value in [10000.0, 10500.0, 9800.0]:
            await storage.save_portfolio_snapshot(
                cash=value, positions_json="[]", total_value=value
            )

        history = await storage.get_portfolio_history(limit=10)
        assert len(history) == 3
        # Most recent first
        assert history[0].total_value == pytest.approx(9800.0)
        assert history[-1].total_value == pytest.approx(10000.0)

    @pytest.mark.asyncio
    async def test_get_max_portfolio_value(self, storage: Storage) -> None:
        """MAX(total_value) over all snapshots — the drawdown high-water seed [§7.5]."""
        for value in [10_000.0, 12_500.0, 9_800.0]:
            await storage.save_portfolio_snapshot(
                cash=value, positions_json="[]", total_value=value
            )
        assert await storage.get_max_portfolio_value() == pytest.approx(12_500.0)

    @pytest.mark.asyncio
    async def test_get_max_portfolio_value_empty(self, storage: Storage) -> None:
        assert await storage.get_max_portfolio_value() is None


class TestRehydrationQueries:
    """Storage seams used by startup rehydration (§7.7)."""

    @pytest.mark.asyncio
    async def test_get_closed_decisions_filters_and_orders(self, storage: Storage) -> None:
        open_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.9,
            reasoning="open trade",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        closed_ids = []
        for pnl in (-5.0, 3.0):
            decision_id = await storage.save_llm_decision(
                symbol="BTC/USDT",
                action="sell",
                confidence=0.9,
                reasoning="closed trade",
                stop_loss=None,
                take_profit=None,
                risk_verdict="approved",
                risk_reason=None,
            )
            await storage.set_realized_pnl(decision_id, pnl)
            closed_ids.append(decision_id)

        rows = await storage.get_closed_decisions(limit=10)
        assert [r.id for r in rows] == list(reversed(closed_ids))  # newest first
        assert open_id not in [r.id for r in rows]

    @pytest.mark.asyncio
    async def test_get_first_portfolio_snapshot_of_day(self, storage: Storage) -> None:
        await storage.save_portfolio_snapshot(
            cash=9_000.0, positions_json="[]", total_value=9_000.0
        )
        await storage.save_portfolio_snapshot(
            cash=8_700.0, positions_json="[]", total_value=8_700.0
        )

        first = await storage.get_first_portfolio_snapshot_of_day()
        assert first is not None
        assert first.total_value == pytest.approx(9_000.0)  # earliest of today

    @pytest.mark.asyncio
    async def test_get_first_portfolio_snapshot_of_day_empty(self, storage: Storage) -> None:
        assert await storage.get_first_portfolio_snapshot_of_day() is None


class TestStorageLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_enables_wal(self, tmp_db_path: str) -> None:
        """WAL lets the dashboard/backtester read while the agent writes."""
        from sqlalchemy import text

        storage = Storage(tmp_db_path)
        await storage.initialize()

        # WAL is a persistent property of the SQLite file — assert it through the
        # async engine the agent actually uses.
        async with storage._engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()

        await storage.close()
        assert str(mode).lower() == "wal"

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()

        # Verify tables exist by inserting into each
        await storage.save_portfolio_snapshot(cash=100.0, positions_json="[]", total_value=100.0)
        snapshot = await storage.get_latest_portfolio_snapshot()
        assert snapshot is not None

        await storage.close()

    @pytest.mark.asyncio
    async def test_close_disposes_engine(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path)
        await storage.initialize()
        assert not storage._closed

        await storage.close()
        assert storage._closed
