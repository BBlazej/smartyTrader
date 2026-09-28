"""Agent-driven BUY → SELL round trip on the OKX **demo** (§7.28) — demo-only CLI.

``scripts.demo_round_trip`` drives the executor directly and writes nothing. This one
runs the **real crypto runner** (``run_agent``: runner lock, agent-scoped storage,
venue tagging, startup rehydration, decision pipeline, risk gate, agent
persistence + reconciliation) twice, with only the LLM replaced by a scripted
client:

1. run 1 — the "LLM" says BUY (stop 5 % below, target 10 % above the live price):
   sized, gated, placed, persisted like any entry;
2. run 2 — a *fresh* runner (a real restart: the ledger is rebuilt from the DB) and
   the "LLM" says SELL: the close, its realized PnL and the entry-decision backfill.

Then it reads back every row the two runs wrote and prints them::

    python -m scripts.demo_agent_round_trip            # dry run: plan only, nothing runs
    python -m scripts.demo_agent_round_trip --yes      # trade on the demo + report
    python -m scripts.demo_agent_round_trip --sell-only --yes   # run 2 only: close what
                                                       # an earlier run left open

Rows land in the **demo book** (``data/demo_crypto.db``, §7.78) under agent ``crypto`` /
venue ``myokx-sandbox`` — that is the point — with the decision reasoning marked
``SMOKE TEST`` so the model's history shows what they were. The runner runs with
``expected_mode=demo``, so this script structurally cannot touch a paper or real book.
Refuses anything but a sandbox executor; trades only ``--symbol`` (watchlist off, one
decision per run regardless of bar timing).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import structlog

from scripts.demo_round_trip import ensure_demo
from scripts.run_crypto_agent import _build_data_and_execution
from src.agents.crypto_agent import CryptoAgent
from src.core import db_layout
from src.core.config import Settings
from src.core.models import Action, TradeSignal
from src.core.runner import build_alerts, load_dotenv, run_agent
from src.data.ccxt_provider import create_ccxt_provider
from src.monitoring import setup_logging

log = structlog.get_logger().bind(component="demo_agent_round_trip")

SMOKE_REASONING = (
    "SMOKE TEST — forced {action} by scripts/demo_agent_round_trip.py (§7.28); not a model decision"
)


class ScriptedLLM:
    """``LLMClient`` stand-in returning one fixed signal (the run trades one symbol only)."""

    last_metrics = None  # no LLM call happened — nothing to record (§7.69)

    def __init__(self, signal: TradeSignal) -> None:
        self._signal = signal
        self.calls = 0

    async def ask_trade_signal(
        self, system_prompt: str, user_prompt: str, json_schema: dict | None = None
    ) -> TradeSignal:
        self.calls += 1
        return self._signal.model_copy()

    async def close(self) -> None:
        return None


def smoke_signal(action: Action, symbol: str, price: float) -> TradeSignal:
    """The forced signal; a BUY carries a valid stop/target around *price* (§7.54)."""
    return TradeSignal(
        symbol=symbol,
        action=action,
        confidence=0.9,
        reasoning=SMOKE_REASONING.format(action=action.value.upper()),
        stop_loss=round(price * 0.95, 1) if action == Action.BUY else None,
        take_profit=round(price * 1.10, 1) if action == Action.BUY else None,
    )


def smoke_settings(settings: Settings, symbol: str) -> Settings:
    """Narrow the live settings to one symbol, watchlist off (in memory only)."""
    settings.crypto_agent.pairs = [symbol]
    watchlist = getattr(settings.crypto_agent, "watchlist", None)
    if watchlist is not None:
        watchlist.enabled = False
    return settings


def _demo_components(settings: Settings) -> tuple[Any, Any]:
    provider, executor, mode = _build_data_and_execution(settings)
    ensure_demo(mode, getattr(executor, "client", None))
    return provider, executor


async def run_phase(settings: Settings, llm: ScriptedLLM) -> None:
    """One full runner lifecycle (``--once``) with the scripted LLM."""
    cfg = settings.crypto_agent
    await run_agent(
        settings,
        component="crypto",
        agent_enabled=True,
        interval_minutes=cfg.interval_minutes,
        decision_history_limit=cfg.decision_history_limit,
        job_id="crypto_cycle",
        timeframe=cfg.timeframe or "1h",
        decide_on_new_bar_only=False,  # the forced decision must not wait for a bar
        build_components=lambda: _demo_components(settings),
        build_agent=lambda pipeline, storage, risk_engine, llm_client: CryptoAgent(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=llm_client,
            pairs=cfg.pairs,
            timeframe=cfg.timeframe or "1h",
            alerts=build_alerts(settings),
        ),
        run_once=True,
        llm_client=llm,
        # §7.78: structurally demo-only — the runner refuses any other mode's executor
        # and opens only data/demo_crypto.db.
        expected_mode=db_layout.DEMO,
    )


def _rows(db: sqlite3.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def high_water_ids(db_path: str) -> dict[str, int]:
    """Current max ids, so the report shows only what this run wrote."""
    if not Path(db_path).exists():  # first smoke run — the demo book is created by run 1
        return {table: 0 for table in ("llm_decisions", "orders", "portfolio_snapshots")}
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
        return {
            table: db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            for table in ("llm_decisions", "orders", "portfolio_snapshots")
        }


def read_back(db_path: str, since: dict[str, int]) -> dict[str, Any]:
    """Every row the two runs wrote, plus the agent's heartbeat."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
        return {
            "decisions": _rows(
                db,
                "SELECT id, timestamp, agent, symbol, action, confidence, stop_loss, take_profit,"
                " risk_verdict, risk_reason, realized_pnl, is_fallback, strategy"
                " FROM llm_decisions WHERE id > ? ORDER BY id",
                since["llm_decisions"],
            ),
            "orders": _rows(
                db,
                "SELECT id, order_id, agent, venue, symbol, side, quantity, price, status,"
                " decision_id, realized_pnl, created_at, filled_at, strategy, fee_base, fee_quote"
                " FROM orders WHERE id > ? ORDER BY id",
                since["orders"],
            ),
            "portfolio_snapshots": _rows(
                db,
                "SELECT id, timestamp, venue, cash, total_value, unrealized_pnl, positions_json"
                " FROM portfolio_snapshots WHERE id > ? ORDER BY id",
                since["portfolio_snapshots"],
            ),
            "agent_control": _rows(
                db,
                "SELECT agent, state, last_cycle_at, last_error FROM agent_control"
                " WHERE agent = 'crypto'",
            ),
        }


async def _live_price(settings: Settings, symbol: str) -> float:
    provider = create_ccxt_provider(exchange_id=settings.crypto_agent.exchange, testnet=False)
    try:
        snapshot = await provider.fetch_snapshot(symbol, settings.crypto_agent.timeframe or "1h")
    finally:
        await provider.close()
    if not snapshot.candles:
        raise SystemExit(f"no live candles for {symbol}")
    return snapshot.candles[-1].close


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    settings = smoke_settings(Settings(), args.symbol)
    setup_logging(settings.monitoring.log_level)
    if not settings.crypto_agent.enabled:
        raise SystemExit("crypto_agent.enabled is false — nothing would run")
    price = await _live_price(settings, args.symbol)
    buy = smoke_signal(Action.BUY, args.symbol, price)
    # The demo book only (§7.78): every row this writes lands in data/demo_crypto.db.
    book = str(db_layout.db_path(settings.storage.data_dir, db_layout.DEMO, "crypto"))
    plan = {
        "symbol": args.symbol,
        "db": book,
        "live_price": price,
        "buy_signal": buy.model_dump(mode="json"),
        "sizing": f"max_position_pct={settings.risk.max_position_pct} of the demo book",
    }
    if not args.yes:
        print(json.dumps(plan, indent=2))
        print("\ndry run — nothing ran. Re-run with --yes to trade on the demo.")
        return 0

    db_path = book
    since = high_water_ids(db_path)
    buy_llm = ScriptedLLM(buy)
    if args.sell_only:
        # E.g. run 1's fill was only confirmed after it exited (a venue timeout):
        # the restart reconciles the pending row, then the SELL closes it.
        log.info("run 1 skipped (--sell-only)", symbol=args.symbol)
    else:
        log.info("run 1: scripted BUY through the real runner", symbol=args.symbol)
        await run_phase(settings, buy_llm)

    # Run 2 is a fresh runner — a real restart: ledger + exit levels from the DB.
    sell_llm = ScriptedLLM(smoke_signal(Action.SELL, args.symbol, price))
    log.info("run 2: restart + scripted SELL", symbol=args.symbol)
    await run_phase(smoke_settings(Settings(), args.symbol), sell_llm)

    report = {"plan": plan, "llm_calls": [buy_llm.calls, sell_llm.calls]}
    report.update(read_back(db_path, since))
    print("\n=== Agent-driven OKX demo round trip (§7.28) ===")
    print(json.dumps(report, indent=2, default=str))
    orders = report["orders"]
    ok = (
        len(orders) == (1 if args.sell_only else 2)
        and all(o["status"] == "filled" for o in orders)
        and orders[-1]["realized_pnl"] is not None
    )
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent-driven BUY → SELL round trip on the OKX demo (§7.28)."
    )
    parser.add_argument("--symbol", default="BTC/EUR", help="demo-tradable pair")
    parser.add_argument(
        "--sell-only",
        action="store_true",
        help="skip run 1: restart + scripted SELL only (closes a position an earlier run left)",
    )
    parser.add_argument("--yes", action="store_true", help="actually run and trade on the demo")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
