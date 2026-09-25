"""Tests for the SQLite storage layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.core.storage import OrderRow, Storage


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


class TestDecisionsInRange:
    """Range queries feeding the decision-replay backtester (§7.14)."""

    @staticmethod
    async def _save_at(
        storage: Storage, symbol: str, days_ago: int, is_fallback: bool = False
    ) -> int:
        from sqlalchemy import text

        rid = await storage.save_llm_decision(
            symbol=symbol,
            action="buy",
            confidence=0.8,
            reasoning="replay input",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
            is_fallback=is_fallback,
        )
        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago)
        async with await storage._session() as session:
            # Bind as a string in SQLAlchemy's SQLite DATETIME format: raw
            # datetime params on text() SQL hit sqlite3's deprecated default
            # adapter (§7.29); typed ORM statements never do.
            await session.execute(
                text("UPDATE llm_decisions SET timestamp = :old WHERE id = :rid"),
                {"old": old.strftime("%Y-%m-%d %H:%M:%S.%f"), "rid": rid},
            )
            await session.commit()
        return rid

    async def test_window_bounds_and_ordering(self, storage: Storage) -> None:
        old = await self._save_at(storage, "BTC/USDT", days_ago=8)
        mid = await self._save_at(storage, "BTC/USDT", days_ago=5)
        recent = await self._save_at(storage, "BTC/USDT", days_ago=2)

        rows = await storage.get_decisions_in_range(
            datetime.now(UTC) - timedelta(days=6), datetime.now(UTC) - timedelta(days=1)
        )

        assert [r.id for r in rows] == [mid, recent]  # oldest first; `old` outside window
        assert old not in [r.id for r in rows]

    async def test_symbol_filter(self, storage: Storage) -> None:
        btc = await self._save_at(storage, "BTC/USDT", days_ago=2)
        await self._save_at(storage, "ETH/USDT", days_ago=2)

        rows = await storage.get_decisions_in_range(
            datetime.now(UTC) - timedelta(days=3),
            datetime.now(UTC),
            symbols=["BTC/USDT"],
        )

        assert [r.id for r in rows] == [btc]

    async def test_fallback_rows_excluded_unless_requested(self, storage: Storage) -> None:
        real = await self._save_at(storage, "BTC/USDT", days_ago=2)
        await self._save_at(storage, "BTC/USDT", days_ago=2, is_fallback=True)

        start = datetime.now(UTC) - timedelta(days=3)
        end = datetime.now(UTC)
        default_rows = await storage.get_decisions_in_range(start, end)
        with_fallback = await storage.get_decisions_in_range(start, end, include_fallback=True)

        assert [r.id for r in default_rows] == [real]
        assert len(with_fallback) == 2


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

    async def test_get_filled_orders_chronological_and_filtered(self, storage: Storage) -> None:
        """§7.25: only fills, oldest first, optional symbol filter."""
        for oid, status, symbol in (
            ("o1", "filled", "BTC/USDT"),
            ("o2", "rejected", "ETH/USDT"),
            ("o3", "filled", "ETH/USDT"),
            ("o4", "pending", "BTC/USDT"),
            ("o5", "filled", "BTC/USDT"),
        ):
            await storage.save_order(
                order_id=oid,
                symbol=symbol,
                side="buy",
                quantity=1.0,
                price=10.0,
                status=status,
            )

        all_fills = await storage.get_filled_orders()
        assert [o.order_id for o in all_fills] == ["o1", "o3", "o5"]

        btc_only = await storage.get_filled_orders("BTC/USDT")
        assert [o.order_id for o in btc_only] == ["o1", "o5"]

    async def test_update_order_status(self, storage: Storage) -> None:
        """§7.28: reconciliation patches status; optional fill fields never blank prior values."""
        await storage.save_order(
            order_id="rc-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            price=100.0,
            status="pending",
        )
        filled_at = datetime(2030, 1, 1, tzinfo=UTC)
        ok = await storage.update_order_status("rc-1", "filled", price=105.5, filled_at=filled_at)
        assert ok is True

        row = next(o for o in await storage.get_recent_orders("BTC/USDT") if o.order_id == "rc-1")
        assert row.status == "filled"
        assert row.price == pytest.approx(105.5)
        assert row.filled_at is not None and row.filled_at.year == 2030

        # A later transition without fill data must not erase the recorded fill.
        assert await storage.update_order_status("rc-1", "cancelled") is True
        row = next(o for o in await storage.get_recent_orders("BTC/USDT") if o.order_id == "rc-1")
        assert row.status == "cancelled"
        assert row.price == pytest.approx(105.5)

        assert await storage.update_order_status("does-not-exist", "filled") is False


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


class TestPruning:
    """Retention pruning (§7.12)."""

    @staticmethod
    async def _backdate(storage: Storage, table: str, column: str, row_id: int, days: int) -> None:
        """Move a row's timestamp back in time (as naive UTC, like the schema stores)."""
        from sqlalchemy import text

        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        async with await storage._session() as session:
            # String bind in SQLAlchemy's SQLite DATETIME format — see §7.29
            # (raw datetime params on text() SQL hit sqlite3's deprecated adapter).
            await session.execute(
                text(f"UPDATE {table} SET {column} = :old WHERE id = :rid"),
                {"old": old.strftime("%Y-%m-%d %H:%M:%S.%f"), "rid": row_id},
            )
            await session.commit()

    async def test_prunes_old_snapshots_keeps_recent(self, storage: Storage) -> None:
        old_id = await storage.save_market_snapshot("BTC/USDT", "5m", "[]")
        recent_id = await storage.save_market_snapshot("BTC/USDT", "5m", "[]")
        await self._backdate(storage, "market_snapshots", "fetched_at", old_id, days=40)

        counts = await storage.prune(snapshot_days=30)

        assert counts["market_snapshots"] == 1
        remaining = {r.id for r in await storage.get_recent_snapshots("BTC/USDT", limit=10)}
        assert remaining == {recent_id}

    async def test_history_window_off_keeps_decisions_and_orders(self, storage: Storage) -> None:
        decision_id = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="old trade",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        await self._backdate(storage, "llm_decisions", "timestamp", decision_id, days=400)

        counts = await storage.prune(snapshot_days=30, history_days=0)

        assert "llm_decisions" not in counts
        recent = await storage.get_recent_decisions("BTC/USDT")
        assert [d.id for d in recent] == [decision_id]

    async def test_history_window_prunes_old_decisions_and_orders(self, storage: Storage) -> None:
        old_decision = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="ancient",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        new_decision = await storage.save_llm_decision(
            symbol="BTC/USDT",
            action="sell",
            confidence=0.7,
            reasoning="recent",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        await self._backdate(storage, "llm_decisions", "timestamp", old_decision, days=200)

        order_id = await storage.save_order(
            order_id="ord-old",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            price=50_000.0,
            status="filled",
        )  # created_at stamped now; backdate it too
        assert order_id > 0
        await self._backdate(storage, "orders", "created_at", order_id, days=200)

        counts = await storage.prune(snapshot_days=0, history_days=90)

        assert counts["llm_decisions"] == 1
        assert counts["orders"] == 1
        remaining = {d.id for d in await storage.get_recent_decisions("BTC/USDT", limit=10)}
        assert remaining == {new_decision}

    async def test_orders_get_created_at_stamped(self, storage: Storage) -> None:
        order_id = await storage.save_order(
            order_id="ord-stamp",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            price=50_000.0,
            status="open",
        )
        async with await storage._session() as session:
            row = await session.get(OrderRow, order_id)
        assert row is not None
        assert row.created_at is not None

    async def test_portfolio_snapshots_are_never_pruned(self, storage: Storage) -> None:
        snap_id = await storage.save_portfolio_snapshot(
            cash=1.0, positions_json="[]", total_value=1.0
        )
        await self._backdate(storage, "portfolio_snapshots", "timestamp", snap_id, days=900)

        counts = await storage.prune(snapshot_days=1, history_days=1)

        assert "portfolio_snapshots" not in counts
        assert await storage.get_max_portfolio_value() == pytest.approx(1.0)

    async def test_disabled_windows_is_a_noop(self, storage: Storage) -> None:
        old_id = await storage.save_market_snapshot("BTC/USDT", "5m", "[]")
        await self._backdate(storage, "market_snapshots", "fetched_at", old_id, days=9999)

        counts = await storage.prune(snapshot_days=0, history_days=0)

        assert counts == {}
        assert len(await storage.get_recent_snapshots("BTC/USDT")) == 1


class TestMigrations:
    """Pre-existing databases get the new columns (§7.12)."""

    async def test_orders_created_at_added_and_backfilled(self, tmp_db_path: str) -> None:
        import sqlite3

        # A database created *before* orders.created_at existed.
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, symbol TEXT, "
            "side TEXT, quantity FLOAT, price FLOAT, status TEXT, decision_id INTEGER, "
            "filled_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO orders (order_id, symbol, side, quantity, price, status, filled_at) "
            "VALUES ('ord-legacy', 'BTC/USDT', 'buy', 1.0, 50000.0, 'filled', "
            "'2026-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        storage = Storage(tmp_db_path)
        await storage.initialize()

        orders = await storage.get_recent_orders("BTC/USDT")
        assert len(orders) == 1
        # Backfilled from filled_at so the legacy row is prunable by the history window.
        assert orders[0].created_at is not None
        assert orders[0].created_at.year == 2026

        await storage.close()

    async def test_agent_column_added_and_backfilled(self, tmp_db_path: str) -> None:
        """Pre-§7.39 rows are attributed: slash symbols → crypto, tickers → stocks."""
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "CREATE TABLE llm_decisions (id INTEGER PRIMARY KEY, symbol TEXT, action TEXT, "
            "confidence FLOAT, reasoning TEXT, stop_loss FLOAT, take_profit FLOAT, "
            "risk_verdict TEXT, risk_reason TEXT, realized_pnl FLOAT, "
            "is_fallback INTEGER NOT NULL DEFAULT 0, timestamp TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, symbol TEXT, "
            "side TEXT, quantity FLOAT, price FLOAT, status TEXT, decision_id INTEGER, "
            "filled_at TIMESTAMP, created_at TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE portfolio_snapshots (id INTEGER PRIMARY KEY, cash FLOAT, "
            "positions_json TEXT, total_value FLOAT, unrealized_pnl FLOAT, timestamp TIMESTAMP)"
        )
        for symbol in ("BTC/USDT", "AAPL"):
            conn.execute(
                "INSERT INTO llm_decisions (symbol, action, confidence, reasoning, risk_verdict, "
                "timestamp) VALUES (?, 'hold', 0.5, 'r', 'approved', '2026-09-01 00:00:00')",
                (symbol,),
            )
            conn.execute(
                "INSERT INTO orders (order_id, symbol, side, quantity, price, status) "
                "VALUES (?, ?, 'buy', 1.0, 10.0, 'filled')",
                (f"o-{symbol}", symbol),
            )
        books = [
            ([{"symbol": "AAPL"}, {"symbol": "MSFT"}], 110.0),  # all tickers → stocks
            ([{"symbol": "BTC/USDT"}], 100.0),  # crypto
            ([], 105.0),  # empty book is ambiguous → crypto (default-enabled agent)
        ]
        for positions, value in books:
            conn.execute(
                "INSERT INTO portfolio_snapshots (cash, positions_json, total_value, "
                "unrealized_pnl, timestamp) VALUES (0, ?, ?, 0, '2026-09-01 00:00:00')",
                (json.dumps(positions), value),
            )
        conn.commit()
        conn.close()

        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            decisions = {d.symbol: d.agent for d in await storage.get_recent_decisions(limit=10)}
            assert decisions == {"BTC/USDT": "crypto", "AAPL": "stocks"}
            orders = {o.symbol: o.agent for o in await storage.get_recent_orders()}
            assert orders == {"BTC/USDT": "crypto", "AAPL": "stocks"}
            assert await storage.get_max_portfolio_value(agent="stocks") == 110.0
            assert await storage.get_max_portfolio_value(agent="crypto") == 105.0
        finally:
            await storage.close()

        # Idempotent: a second startup neither fails nor re-attributes anything.
        again = Storage(tmp_db_path)
        await again.initialize()
        try:
            assert await again.get_max_portfolio_value(agent="stocks") == 110.0
        finally:
            await again.close()


class TestAgentScoping:
    """§7.39: one DB, two agents — a bound Storage only ever sees its own rows."""

    @pytest.fixture()
    async def pair(self, tmp_db_path: str):
        crypto = Storage(tmp_db_path, agent="crypto")
        stocks = Storage(tmp_db_path, agent="stocks")
        unbound = Storage(tmp_db_path)
        await crypto.initialize()
        yield crypto, stocks, unbound
        for s in (crypto, stocks, unbound):
            await s.close()

    async def _decision(self, storage: Storage, symbol: str, pnl: float | None = None) -> int:
        return await storage.save_llm_decision(
            symbol=symbol,
            action="buy",
            confidence=0.8,
            reasoning="r",
            stop_loss=1.0,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
            realized_pnl=pnl,
        )

    async def test_writes_are_stamped_with_the_bound_agent(self, pair) -> None:
        crypto, stocks, unbound = pair
        await self._decision(crypto, "BTC/USDT")
        await self._decision(stocks, "AAPL")
        rows = await unbound.get_recent_decisions(limit=10)
        assert {r.symbol: r.agent for r in rows} == {"BTC/USDT": "crypto", "AAPL": "stocks"}

    async def test_portfolio_reads_are_isolated(self, pair) -> None:
        crypto, stocks, unbound = pair
        await crypto.save_portfolio_snapshot(cash=90.0, positions_json="[]", total_value=100.0)
        await stocks.save_portfolio_snapshot(cash=50.0, positions_json="[]", total_value=110.0)

        assert (await crypto.get_latest_portfolio_snapshot()).total_value == 100.0
        assert (await stocks.get_latest_portfolio_snapshot()).total_value == 110.0
        assert await crypto.get_max_portfolio_value() == 100.0  # not the other agent's peak
        assert (await crypto.get_first_portfolio_snapshot_of_day()).total_value == 100.0
        assert [r.total_value for r in await crypto.get_portfolio_history()] == [100.0]
        # Unbound (dashboard/tools) sees everything; an explicit agent narrows it.
        assert await unbound.get_max_portfolio_value() == 110.0
        assert await unbound.get_max_portfolio_value(agent="crypto") == 100.0

    async def test_decision_and_order_reads_are_isolated(self, pair) -> None:
        crypto, stocks, _ = pair
        await self._decision(crypto, "BTC/USDT", pnl=-5.0)
        await self._decision(stocks, "AAPL", pnl=-7.0)
        await crypto.save_order("c1", "BTC/USDT", "buy", 1.0, 10.0, "filled")
        await stocks.save_order("s1", "AAPL", "buy", 2.0, 20.0, "filled")

        assert [r.symbol for r in await crypto.get_closed_decisions()] == ["BTC/USDT"]
        assert [r.symbol for r in await stocks.get_recent_decisions(limit=10)] == ["AAPL"]
        assert [o.order_id for o in await crypto.get_filled_orders()] == ["c1"]
        assert [o.order_id for o in await stocks.get_recent_orders()] == ["s1"]
        start, end = datetime.now(UTC) - timedelta(days=1), datetime.now(UTC) + timedelta(days=1)
        assert [r.symbol for r in await stocks.get_decisions_in_range(start, end)] == ["AAPL"]


class TestClosingFills:
    """§7.46: closing-fill outcomes on the orders table."""

    async def test_newest_first_and_scoped(self, tmp_db_path: str) -> None:
        crypto = Storage(tmp_db_path, agent="crypto")
        stocks = Storage(tmp_db_path, agent="stocks")
        await crypto.initialize()
        try:
            await crypto.save_order("c1", "BTC/USDT", "sell", 1, 10, "filled", realized_pnl=-1.0)
            await crypto.save_order("c2", "BTC/USDT", "buy", 1, 10, "filled")  # no outcome
            await crypto.save_order("c3", "BTC/USDT", "sell", 1, 10, "filled", realized_pnl=2.0)
            await crypto.save_order("c4", "BTC/USDT", "sell", 1, 10, "rejected", realized_pnl=9.0)
            await stocks.save_order("s1", "AAPL", "sell", 1, 10, "filled", realized_pnl=-3.0)
            assert [o.order_id for o in await crypto.get_recent_closing_fills()] == ["c3", "c1"]
            assert await crypto.update_order_status("c2", "filled", realized_pnl=0.5)
            assert [o.order_id for o in await crypto.get_recent_closing_fills()] == [
                "c3",
                "c2",
                "c1",
            ]
        finally:
            await crypto.close()
            await stocks.close()

    async def test_legacy_orders_table_gains_the_column(self, tmp_db_path: str) -> None:
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, symbol TEXT, "
            "side TEXT, quantity FLOAT, price FLOAT, status TEXT, decision_id INTEGER, "
            "filled_at TIMESTAMP, created_at TIMESTAMP)"
        )
        conn.commit()
        conn.close()
        storage = Storage(tmp_db_path)
        await storage.initialize()
        try:
            await storage.save_order("x", "BTC/USDT", "sell", 1, 10, "filled", realized_pnl=-1.0)
            assert (await storage.get_recent_closing_fills())[0].realized_pnl == -1.0
        finally:
            await storage.close()


class TestVenueTagging:
    """§7.61: order/portfolio rows carry the execution venue; replay reads filter on it."""

    async def test_filled_quantity_is_persisted_on_reconcile(self, storage: Storage) -> None:
        await storage.save_order("p-1", "BTC/USDT", "buy", 1.0, 100.0, "pending")
        assert await storage.update_order_status("p-1", "filled", price=101.0, quantity=0.3)
        (row,) = await storage.get_filled_orders()
        assert row.quantity == pytest.approx(0.3)

    async def test_writes_are_stamped_and_reads_filter(self, tmp_db_path: str) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        try:
            await storage.save_order("legacy", "BTC/USDT", "buy", 1.0, 1.0, "filled")
            storage.bind_venue("paper")
            await storage.save_order("paper-1", "BTC/USDT", "buy", 1.0, 1.0, "filled")
            await storage.save_portfolio_snapshot(
                cash=100.0, positions_json="[]", total_value=100.0
            )
            storage.bind_venue("kraken-live")
            await storage.save_order("K-1", "BTC/USDT", "buy", 1.0, 1.0, "filled")
            await storage.save_order("K-2", "BTC/USDT", "buy", 1.0, 1.0, "pending")
            await storage.save_portfolio_snapshot(cash=7.0, positions_json="[]", total_value=7.0)

            ids = lambda rows: [r.order_id for r in rows]
            assert ids(await storage.get_filled_orders(venue="paper")) == ["legacy", "paper-1"]
            assert ids(await storage.get_filled_orders(venue="kraken-live")) == ["legacy", "K-1"]
            assert ids(await storage.get_filled_orders()) == ["legacy", "paper-1", "K-1"]
            assert ids(await storage.get_pending_orders(venue="paper")) == []
            assert ids(await storage.get_pending_orders(venue="kraken-live")) == ["K-2"]

            assert (await storage.get_latest_portfolio_snapshot(venue="paper")).cash == 100.0
            assert (await storage.get_latest_portfolio_snapshot()).cash == 7.0
            assert (await storage.get_latest_portfolio_snapshot()).venue == "kraken-live"
        finally:
            await storage.close()

    async def test_migration_adds_venue_and_tags_paper_orders(self, tmp_db_path: str) -> None:
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, symbol TEXT, "
            "side TEXT, quantity FLOAT, price FLOAT, status TEXT, decision_id INTEGER, "
            "filled_at TIMESTAMP, created_at TIMESTAMP, agent VARCHAR(20), realized_pnl FLOAT)"
        )
        conn.execute(
            "CREATE TABLE portfolio_snapshots (id INTEGER PRIMARY KEY, cash FLOAT, "
            "positions_json TEXT, total_value FLOAT, unrealized_pnl FLOAT, timestamp TIMESTAMP, "
            "agent VARCHAR(20))"
        )
        for oid in ("paper-abc", "OXYZ-1"):
            conn.execute(
                "INSERT INTO orders (order_id, symbol, side, quantity, price, status) "
                "VALUES (?, 'BTC/USDT', 'buy', 1.0, 10.0, 'filled')",
                (oid,),
            )
        conn.execute(
            "INSERT INTO portfolio_snapshots (cash, positions_json, total_value, unrealized_pnl, "
            "timestamp) VALUES (5, '[]', 5, 0, '2026-09-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        for _ in range(2):  # idempotent
            storage = Storage(tmp_db_path)
            await storage.initialize()
            try:
                venues = {o.order_id: o.venue for o in await storage.get_recent_orders()}
                assert venues == {"paper-abc": "paper", "OXYZ-1": None}
                snap = await storage.get_latest_portfolio_snapshot(venue="xtb-demo")
                assert snap is not None and snap.venue is None  # legacy rows match any venue
            finally:
                await storage.close()
