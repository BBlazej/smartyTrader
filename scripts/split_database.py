"""One-time split of the legacy shared DB into per-agent × mode books (§7.78).

Before §7.78 everything lived in one SQLite file (``storage.database_path``), kept
apart only by ``agent``/``venue`` filters. This script splits it into the book files
of :mod:`src.core.db_layout` — ``<data_dir>/<mode>_<agent>.db`` — and leaves the
original **untouched** as the backup (plus an explicit timestamped online backup):

    python -m scripts.split_database                 # dry run: print the plan, write nothing
    python -m scripts.split_database --yes           # apply (books must not exist yet)

Routing rules (§7.78 "Migration"):
* ``orders`` / ``portfolio_snapshots`` / ``strategy_allocations`` → their ``venue``
  column decides the mode (:func:`src.core.db_layout.venue_mode`); NULL venue
  (legacy paper rows) → ``paper``.
* ``llm_decisions`` has no venue: it follows **its own orders'** venue; a decision
  without orders follows the venue of the portfolio snapshot of *its cycle* (the
  nearest same-agent snapshot, ≤90 s — runners stamp one per cycle); else ``paper``.
* ``agent_control`` / ``watchlist_entries`` / ``sleeve_drawdown_resets`` are
  agent-keyed without venue → copied into every book of that agent being created.
* ``market_snapshots`` is a re-creatable candle cache — not migrated (runners rebuild it).
* The §7.28 smoke-test rows are **dropped** (decided 2026-09-28): decisions whose
  reasoning starts with ``SMOKE TEST``, their orders, and the portfolio snapshots
  those runs wrote (same agent+venue, within 120 s of a dropped decision). Pass
  ``--keep-smoke`` to migrate them instead.

Original row ids are preserved so ``orders.decision_id`` links stay valid; each new
file is stamped with its ``(agent, mode)`` identity and WAL-enabled. Files that
already exist abort the run (nothing is overwritten); the source is never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import create_engine, text

from src.core.config import Settings
from src.core.db_layout import UnknownVenueError, book_key, db_path, venue_mode
from src.core.storage.models import Base
from src.monitoring import setup_logging

#: Decisions whose reasoning starts with this are §7.28 smoke-test rows (dropped).
SMOKE_PREFIX = "SMOKE TEST"
#: A decision's own cycle stamped a portfolio snapshot within this many seconds.
CYCLE_WINDOW_SECONDS = 90.0
#: Snapshots written within this window of a dropped smoke decision go with it.
SMOKE_SNAPSHOT_WINDOW_SECONDS = 120.0

#: Tables copied verbatim, split by their ``venue`` column (NULL → paper).
VENUE_TABLES = ("orders", "portfolio_snapshots", "strategy_allocations", "drawdown_resets")
#: Agent-keyed tables without venue, copied into every created book of that agent.
AGENT_TABLES = ("agent_control", "watchlist_entries", "sleeve_drawdown_resets")

#: Everything the legacy file may hold (``market_snapshots`` is a re-creatable cache,
#: ``db_identity`` belongs to the new files only — neither is read here).
_ALL_TABLES = VENUE_TABLES + ("llm_decisions",) + AGENT_TABLES


@dataclass
class Bucket:
    """Rows destined for one ``(mode, agent)`` book."""

    mode: str
    agent: str
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def add(self, table: str, row: dict[str, Any]) -> None:
        self.rows.setdefault(table, []).append(row)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not _has_table(conn, table):
        return []  # a pre-migration legacy file may lack the newer tables
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def plan_split(conn: sqlite3.Connection, keep_smoke: bool = False) -> dict[str, Bucket]:
    """Read the legacy DB and bucket every row for its target book (writes nothing)."""
    buckets: dict[tuple[str, str], Bucket] = {}

    def bucket_for(mode: str, agent: str) -> Bucket:
        key = (mode, agent or "unknown")
        if key not in buckets:
            buckets[key] = Bucket(mode, agent or "unknown")
        return buckets[key]

    tables: dict[str, list[dict[str, Any]]] = {t: _rows(conn, t) for t in _ALL_TABLES}
    orders, decisions, snapshots = (
        tables["orders"],
        tables["llm_decisions"],
        tables["portfolio_snapshots"],
    )

    # ── smoke-test drop (§7.28 rows, decided out of the migration) ──────────────
    smoke_ids = {d["id"] for d in decisions if str(d.get("reasoning", "")).startswith(SMOKE_PREFIX)}
    dropped: dict[str, int] = {}
    if smoke_ids and not keep_smoke:
        keep_orders, smoke_orders = [], []
        for o in orders:
            (smoke_orders if o.get("decision_id") in smoke_ids else keep_orders).append(o)
        # Their cycle snapshots: same agent+venue within the window of a dropped decision.
        smoke_by_agent: dict[str | None, list[datetime]] = {}
        for d in decisions:
            if d["id"] in smoke_ids:
                stamp = _parse_ts(d.get("timestamp"))
                if stamp is not None:
                    smoke_by_agent.setdefault(d.get("agent"), []).append(stamp)
        kept_snapshots, smoke_snapshots = [], []
        for s in snapshots:
            stamps = smoke_by_agent.get(s.get("agent"), [])
            stamp = _parse_ts(s.get("timestamp"))
            near = stamp is not None and any(
                abs((stamp - other).total_seconds()) <= SMOKE_SNAPSHOT_WINDOW_SECONDS
                for other in stamps
            )
            (smoke_snapshots if near else kept_snapshots).append(s)
        decisions = [d for d in decisions if d["id"] not in smoke_ids]
        dropped = {
            "llm_decisions": len(smoke_ids),
            "orders": len(smoke_orders),
            "portfolio_snapshots": len(smoke_snapshots),
        }
        orders, snapshots = keep_orders, kept_snapshots
        tables["orders"], tables["llm_decisions"], tables["portfolio_snapshots"] = (
            orders,
            decisions,
            snapshots,
        )
    elif smoke_ids:
        dropped = {"llm_decisions": 0, "orders": 0, "portfolio_snapshots": 0}

    # ── venue-table routing: their own venue column (NULL → paper) ──────────────
    order_mode: dict[int, str] = {}  # decision id → mode, for decisions following orders
    for table in VENUE_TABLES:
        for row in tables[table]:
            agent = row.get("agent") or ""
            mode = venue_mode(row.get("venue"))
            bucket_for(mode, agent).add(table, row)
            if table == "orders" and row.get("decision_id") is not None:
                order_mode[row["decision_id"]] = mode

    # ── decisions: own orders' venue → their cycle's snapshot venue → paper ─────
    snap_index: dict[str | None, list[tuple[datetime, str]]] = {}
    for s in snapshots:
        stamp = _parse_ts(s.get("timestamp"))
        if stamp is not None:
            snap_index.setdefault(s.get("agent"), []).append((stamp, venue_mode(s.get("venue"))))

    for d in decisions:
        agent = d.get("agent") or ""
        mode = order_mode.get(d["id"])
        if mode is None:
            stamp = _parse_ts(d.get("timestamp"))
            candidates = snap_index.get(d.get("agent"), [])
            if stamp is not None and candidates:
                near_stamp, near_mode = min(
                    candidates, key=lambda c: abs((c[0] - stamp).total_seconds())
                )
                if abs((near_stamp - stamp).total_seconds()) <= CYCLE_WINDOW_SECONDS:
                    mode = near_mode
        if mode is None:
            mode = "paper"
        bucket_for(mode, agent).add("llm_decisions", d)

    # ── agent-keyed tables (no venue): copy into every created book of the agent ─
    for table in AGENT_TABLES:
        for row in tables[table]:
            agent = row.get("agent") or ""
            for bucket in [b for (m, a), b in buckets.items() if a == agent]:
                bucket.add(table, row)

    result = {book_key(b.mode, b.agent): b for b in buckets.values()}
    if dropped and any(dropped.values()):
        log = structlog.get_logger().bind(component="split_database")
        log.info("smoke-test rows dropped (§7.28)", **dropped)
    return result


def _create_book(path: Path, agent: str, mode: str) -> sqlite3.Connection:
    """Create an empty book with the full schema, WAL on, identity stamped."""
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
    sync_engine.dispose()
    db = sqlite3.connect(str(path))
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    db.execute(
        "INSERT INTO db_identity (id, agent, mode, created_at) VALUES (1, ?, ?, ?)",
        (agent, mode, now),
    )
    db.commit()
    return db


def _copy_rows(db: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    for row in rows:
        keep = {k: v for k, v in row.items() if k in cols}
        placeholders = ", ".join(f":{k}" for k in keep)
        db.execute(f"INSERT INTO {table} ({', '.join(keep)}) VALUES ({placeholders})", keep)
    db.commit()


def execute_split(plan: dict[str, Bucket], data_dir: Path, source: Path) -> list[Path]:
    """Create the books from a plan (refusing existing files) and stamp identities."""
    log = structlog.get_logger().bind(component="split_database")
    targets = []
    for key, bucket in sorted(plan.items()):
        target = db_path(data_dir, bucket.mode, bucket.agent)
        if target.exists():
            raise SystemExit(f"{target} already exists — refusing to overwrite a book (§7.78)")
        targets.append((key, bucket, target))

    backup = _backup(source, data_dir)
    log.info("source backed up", path=str(backup), original_left_untouched=str(source))

    written: list[Path] = []
    for key, bucket, target in targets:
        with closing(_create_book(target, bucket.agent, bucket.mode)) as db:
            for table, rows in bucket.rows.items():
                _copy_rows(db, table, rows)
        counts = {t: len(r) for t, r in bucket.rows.items() if r}
        log.info(
            "book written",
            book=key,
            path=str(target),
            **{f"rows_{table}": n for table, n in counts.items()},
        )
        written.append(target)
    return written


def _backup(source: Path, data_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    dest = data_dir / "backups" / f"{source.stem}-pre-split-{stamp}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(dest))) as dst:
        src.backup(dst)
    return dest


def _print_plan(plan: dict[str, Bucket]) -> None:
    print("\n=== §7.78 split plan (dry run — nothing written) ===")
    if not plan:
        print("no rows to migrate")
        return
    tables = sorted({t for b in plan.values() for t in b.rows})
    header = f"{'book':<18}" + "".join(f"{t:>24}" for t in tables)
    print(header)
    for key, bucket in sorted(plan.items()):
        row = f"{key:<18}" + "".join(f"{len(bucket.rows.get(t, [])):>24}" for t in tables)
        print(row)


async def run(args: argparse.Namespace) -> None:
    settings = Settings(args.config) if args.config else Settings()
    setup_logging(settings.monitoring.log_level)
    log = structlog.get_logger().bind(component="split_database")

    source = Path(args.source or settings.storage.database_path or "")
    if not str(source) or source == Path(":memory:") or not source.exists():
        raise SystemExit(f"legacy database not found at {source!s} — nothing to split")
    data_dir = Path(args.data_dir or settings.storage.data_dir)

    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as conn:
        try:
            plan = plan_split(conn, keep_smoke=args.keep_smoke)
        except UnknownVenueError as exc:
            raise SystemExit(f"cannot route a row: {exc}") from exc
    _print_plan(plan)

    if not args.yes:
        print("\ndry run — re-run with --yes to write the books (the original stays untouched).")
        return
    written = execute_split(plan, data_dir, source)
    log.info("split complete", books=[str(p) for p in written])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split the legacy shared DB into per-agent × mode books (§7.78)."
    )
    parser.add_argument("--source", default=None, help="Legacy DB (default: storage.database_path)")
    parser.add_argument("--data-dir", default=None, help="Target dir (default: storage.data_dir)")
    parser.add_argument("--config", default=None, help="Path to an alternative settings.yaml")
    parser.add_argument(
        "--keep-smoke",
        action="store_true",
        help=f"Migrate '{SMOKE_PREFIX}…' rows instead of dropping them (§7.28)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Actually write the books (default: dry run)"
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
