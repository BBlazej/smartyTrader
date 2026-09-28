"""§7.78 one-time split: routing, smoke-row drop, identities, refusal of existing books."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.split_database import execute_split, plan_split
from src.core.db_layout import db_path


def _ts(minutes_ago: float) -> str:
    return (
        (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(tzinfo=None).isoformat(sep=" ")
    )


def _make_legacy(path: Path) -> None:
    """A legacy shared DB with the *current* production schema (post-migration) but no identity."""
    from sqlalchemy import create_engine

    from src.core.storage.models import Base

    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with closing(sqlite3.connect(path)) as db:

        def decision(i: int, symbol: str, action: str, reasoning: str, when: str, agent: str):
            db.execute(
                "INSERT INTO llm_decisions (id, symbol, action, confidence, reasoning,"
                " risk_verdict, timestamp, is_fallback, agent) VALUES (?,?,?,?,?,?,?,0,?)",
                (i, symbol, action, 0.9, reasoning, "approved", when, agent),
            )

        def order(i: int, oid: str, symbol: str, side: str, did: int, when: str, venue: str):
            db.execute(
                "INSERT INTO orders (id, order_id, symbol, side, quantity, status,"
                " decision_id, created_at, agent, venue) VALUES (?,?,?,?,1,'filled',?,?,?,?)",
                (i, oid, symbol, side, did, when, "crypto", venue),
            )

        def snapshot(i: int, total: float, when: str, venue: str | None):
            db.execute(
                "INSERT INTO portfolio_snapshots (id, cash, positions_json, total_value,"
                " unrealized_pnl, timestamp, agent, venue) VALUES (?,?,'[]',?,0.0,?,'crypto',?)",
                (i, total, total, when, venue),
            )

        # decision 1: paper order attached
        decision(1, "BTC/EUR", "buy", "go", _ts(500), "crypto")
        order(1, "o-1", "BTC/EUR", "buy", 1, _ts(500), "paper")
        # decision 2: no order, but a demo (sandbox) cycle snapshot within the window
        decision(2, "BTC/EUR", "hold", "wait", _ts(300), "crypto")
        snapshot(1, 4600.0, _ts(299.5), "myokx-sandbox")
        # decision 3: no order, nothing near → paper fallback
        decision(3, "ETH/EUR", "hold", "n/a", _ts(900), "crypto")
        # decision 4: SMOKE TEST + its order + its cycle snapshot → all dropped
        decision(4, "BTC/EUR", "buy", "SMOKE TEST — forced BUY", _ts(10), "crypto")
        order(2, "o-2", "BTC/EUR", "buy", 4, _ts(10), "myokx-sandbox")
        snapshot(2, 4599.0, _ts(9.9), "myokx-sandbox")
        # a normal demo snapshot far from the smoke decision → kept
        snapshot(3, 4601.0, _ts(280), "myokx-sandbox")
        # stocks rows: legacy NULL venue → paper_stocks
        decision(5, "AAPL", "sell", "out", _ts(700), "stocks")
        db.execute(
            "INSERT INTO watchlist_entries (id, agent, symbol, source, added_at, expires_at,"
            " meta_json) VALUES (1,'crypto','NEAR/EUR','screener',?,'2099-01-01','{}')",
            (_ts(60),),
        )
        db.execute(
            "INSERT INTO agent_control (agent, state, close_all_requested, updated_at)"
            " VALUES ('crypto','paused',0,?)",
            (_ts(5),),
        )
        db.commit()


@pytest.fixture()
def legacy(tmp_path: Path) -> Path:
    path = tmp_path / "trading_agent.db"
    _make_legacy(path)
    return path


def _book_rows(book: Path, table: str) -> list[tuple]:
    with closing(sqlite3.connect(f"file:{book}?mode=ro", uri=True)) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


class TestPlanRouting:
    def test_every_row_lands_in_its_mode_book(self, legacy: Path) -> None:
        with closing(sqlite3.connect(legacy)) as db:
            plan = plan_split(db)
        assert set(plan) == {"paper_crypto", "demo_crypto", "paper_stocks"}
        paper, demo = plan["paper_crypto"], plan["demo_crypto"]
        assert [r["id"] for r in paper.rows["llm_decisions"]] == [1, 3]
        assert [r["id"] for r in demo.rows["llm_decisions"]] == [2]
        assert [r["id"] for r in paper.rows["orders"]] == [1]
        assert "orders" not in demo.rows  # its only order was the smoke one
        assert [r["id"] for r in demo.rows["portfolio_snapshots"]] == [1, 3]

    def test_smoke_rows_are_dropped_but_far_snapshots_survive(self, legacy: Path) -> None:
        with closing(sqlite3.connect(legacy)) as db:
            plan = plan_split(db)
        all_decisions = {r["id"] for b in plan.values() for r in b.rows.get("llm_decisions", [])}
        all_orders = {r["id"] for b in plan.values() for r in b.rows.get("orders", [])}
        assert 4 not in all_decisions and 2 not in all_orders

    def test_keep_smoke_migrates_them(self, legacy: Path) -> None:
        with closing(sqlite3.connect(legacy)) as db:
            plan = plan_split(db, keep_smoke=True)
        assert any(r["id"] == 4 for r in plan["demo_crypto"].rows["llm_decisions"])

    def test_agent_tables_copied_into_every_book_of_the_agent(self, legacy: Path) -> None:
        with closing(sqlite3.connect(legacy)) as db:
            plan = plan_split(db)
        assert len(plan["paper_crypto"].rows["agent_control"]) == 1
        assert len(plan["demo_crypto"].rows["agent_control"]) == 1
        assert "agent_control" not in plan["paper_stocks"].rows


class TestExecuteSplit:
    def test_books_created_with_identity_and_rows(self, legacy: Path, tmp_path: Path) -> None:
        data_dir = tmp_path / "books"
        with closing(sqlite3.connect(legacy)) as db:
            plan = plan_split(db)
        written = execute_split(plan, data_dir, legacy)
        assert {p.name for p in written} == {"paper_crypto.db", "demo_crypto.db", "paper_stocks.db"}

        demo = db_path(data_dir, "demo", "crypto")
        with closing(sqlite3.connect(demo)) as db:
            assert db.execute("SELECT agent, mode FROM db_identity").fetchone() == (
                "crypto",
                "demo",
            )
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert [r[1] for r in _book_rows(demo, "llm_decisions")] == ["BTC/EUR"]

        # ids preserved → orders.decision_id links stay valid inside one book
        paper = db_path(data_dir, "paper", "crypto")
        with closing(sqlite3.connect(paper)) as db:
            order = db.execute("SELECT id, decision_id FROM orders").fetchone()
            assert order == (1, 1)
            assert db.execute("SELECT COUNT(*) FROM llm_decisions WHERE id = 1").fetchone()[0] == 1

    def test_existing_book_aborts_before_writing_anything(
        self, legacy: Path, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "books"
        data_dir.mkdir()
        db_path(data_dir, "paper", "crypto").touch()
        with closing(sqlite3.connect(legacy)) as db:
            plan = plan_split(db)
        with pytest.raises(SystemExit, match="already exists"):
            execute_split(plan, data_dir, legacy)
        assert not db_path(data_dir, "demo", "crypto").exists()

    def test_source_is_never_modified(self, legacy: Path, tmp_path: Path) -> None:
        before = legacy.read_bytes()
        with closing(sqlite3.connect(legacy)) as db:
            plan = plan_split(db)
        execute_split(plan, tmp_path / "books", legacy)
        assert legacy.read_bytes() == before
        backups = list((tmp_path / "books" / "backups").glob("trading_agent-pre-split-*.db"))
        assert len(backups) == 1
