"""Rehydrate in-memory trading state from SQLite at startup (§7.7).

``PaperExecutor`` keeps its book (cash + positions) and ``RiskEngine`` its trackers
(daily-loss baseline, losing streak, cooldown) only in memory — while decisions,
orders and portfolio snapshots persist. Without this module every restart silently:

* reset paper cash to ``execution.initial_cash`` and dropped open positions
  (persisted decisions then referenced trades that no longer existed), and
* zeroed the daily-loss baseline and the losing-streak/cooldown state —
  weakening every stateful guard exactly when it matters.

Venue executors (Kraken/XTB) get their local FIFO ledger, exit levels and pending
orders back the same way (§7.58).

The runners call :func:`rehydrate_from_storage` once, after storage is
initialized and before any cycle runs. Everything here is fail-soft: a broken
row must never prevent the agent from starting (a fresh-but-safe state beats no
agent at all), so failures log and leave that piece of state defaulted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from ..execution.kraken_executor import PendingOrderRecord
from ..execution.position_tracker import FillRecord
from .models import OrderSide, Position
from .risk_engine import RiskEngine
from .storage import Storage

logger = structlog.get_logger()


def _as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to naive stored timestamps (SQLite has no tz info)."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def rehydrate_paper_executor(executor: Any, storage: Storage) -> bool:
    """Restore a paper executor's cash + positions from the latest snapshot.

    Returns whether state was applied. Executors that report live venue books
    (Kraken/XTB) have no ``load_portfolio_state`` hook and are skipped.
    """
    load = getattr(executor, "load_portfolio_state", None)
    if not callable(load):
        return False
    try:
        row = await storage.get_latest_portfolio_snapshot()
        if row is None:
            return False
        positions = [Position(**p) for p in json.loads(row.positions_json or "[]")]

        # Replay historical fills into the FIFO ledger so open positions keep
        # their per-lot basis + entry decision ids across restarts (§7.25).
        # Fail-soft: without the history the executor falls back to one
        # synthetic lot per position, i.e. pre-§7.25 behavior.
        fills: list[FillRecord] | None = None
        try:
            fills = [
                FillRecord(
                    symbol=o.symbol,
                    side=o.side,
                    quantity=float(o.quantity),
                    price=float(o.price),
                    decision_id=o.decision_id,
                )
                for o in await storage.get_filled_orders()
                if o.price is not None
            ]
        except Exception as exc:  # noqa: BLE001 — fill history is best-effort
            logger.warning("failed to load filled-order history", error=str(exc))

        counts = load(cash=float(row.cash), positions=positions, fills=fills)
    except Exception as exc:  # noqa: BLE001 — startup must not die on a bad row
        logger.warning("failed to rehydrate paper portfolio; starting fresh", error=str(exc))
        return False
    logger.info(
        "paper portfolio rehydrated from storage",
        cash=float(row.cash),
        positions=len(positions),
        replayed_fills=(counts or {}).get("replayed_fills"),
        synthetic_lots=(counts or {}).get("synthetic_lots"),
    )
    return True


def _is_paper_order(order_id: str) -> bool:
    """Paper fills (``paper-…`` ids) never happened at a venue (§7.58)."""
    return order_id.startswith("paper-")


async def rehydrate_venue_executor(executor: Any, storage: Storage) -> None:
    """Restore a venue executor's FIFO ledger, exit levels and pending orders (§7.58).

    Venue executors report live books, but their *local* state — FIFO lots (realized
    PnL + entry attribution on closes; Kraken spot positions themselves, §7.41), the
    entry SL/TP they enforce (§7.9) and orders left open (§7.28) — was memory-only,
    so a restart silently dropped stops and left open orders ``pending`` forever.

    * ``load_fills(fills)`` ← this agent's filled venue orders (paper-era fills of the
      same agent are skipped — they never happened at the venue), with the latest
      buy per symbol carrying its entry decision's SL/TP.
    * ``load_pending_orders(orders)`` ← this agent's ``pending`` rows; the first cycle's
      reconciliation then resolves them.

    Each hook is optional and fail-soft: a failure logs and leaves that piece empty.
    """
    load_fills = getattr(executor, "load_fills", None)
    if callable(load_fills):
        try:
            rows = [
                o
                for o in await storage.get_filled_orders()
                if o.price is not None and not _is_paper_order(o.order_id)
            ]
            last_buy: dict[str, int] = {}
            for o in rows:
                if o.side == "buy" and o.decision_id is not None:
                    last_buy[o.symbol] = o.decision_id
            levels = await storage.get_exit_levels(list(last_buy.values()))
            fills: list[FillRecord] = []
            for o in rows:
                # Only each symbol's latest buy needs its levels (replay keeps the last).
                sl, tp = (
                    levels.get(o.decision_id or -1, (None, None))
                    if o.side == "buy"
                    else (None, None)
                )
                fills.append(
                    FillRecord(
                        symbol=o.symbol,
                        side=o.side,
                        quantity=float(o.quantity),
                        price=float(o.price),
                        decision_id=o.decision_id,
                        stop_loss=sl,
                        take_profit=tp,
                    )
                )
            counts = load_fills(fills)
            logger.info(
                "venue fill ledger rehydrated from storage",
                replayed_fills=(counts or {}).get("replayed_fills"),
                open_symbols=(counts or {}).get("open_symbols"),
            )
        except Exception as exc:  # noqa: BLE001 — startup must not die on a bad row
            logger.warning("failed to rehydrate venue fill ledger", error=str(exc))

    load_pending = getattr(executor, "load_pending_orders", None)
    if callable(load_pending):
        try:
            rows = [
                o
                for o in await storage.get_pending_orders()
                if o.order_id and not _is_paper_order(o.order_id)
            ]
            levels = await storage.get_exit_levels(
                [o.decision_id for o in rows if o.decision_id is not None]
            )
            pending: list[PendingOrderRecord] = []
            for o in rows:
                sl, tp = levels.get(o.decision_id or -1, (None, None))
                pending.append(
                    PendingOrderRecord(
                        order_id=o.order_id,
                        symbol=o.symbol,
                        side=OrderSide(o.side),
                        quantity=float(o.quantity),
                        decision_id=o.decision_id,
                        stop_loss=sl,
                        take_profit=tp,
                    )
                )
            if pending:
                load_pending(pending)
                logger.info("pending venue orders re-tracked from storage", count=len(pending))
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to reload pending venue orders", error=str(exc))


async def rehydrate_risk_engine(risk_engine: RiskEngine, storage: Storage) -> None:
    """Rebuild the daily-loss baseline and the losing-streak/cooldown state.

    * Baseline ← ``total_value`` of today's earliest portfolio snapshot (absent
      → left unset so it seeds lazily from the first fresh reading).
    * Losing streak ← trailing run of **closing fills** (newest first, §7.46) with a
      negative realized PnL — the same one-outcome-per-closing-fill the live tracker
      counts (decision rows double-counted: an LLM round trip stamps both the SELL and
      its entry). Databases with no recorded closing-fill outcomes yet (pre-§7.46)
      fall back to *entry* (BUY) decisions only. At the configured threshold
      (``risk.consecutive_losses_threshold``, §7.26 — never a hardcoded 3) the
      cooldown restarts from the newest loss's timestamp +
      ``consecutive_losses_cooldown_minutes`` if it has not already elapsed.
    """
    try:
        first_today = await storage.get_first_portfolio_snapshot_of_day()
        if first_today is not None:
            risk_engine.restore_daily_baseline(float(first_today.total_value))
            logger.info(
                "daily-loss baseline rehydrated",
                baseline=float(first_today.total_value),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to rehydrate daily-loss baseline", error=str(exc))

    try:
        outcomes: list[tuple[float, datetime | None]] = [
            (float(o.realized_pnl), _as_utc(o.filled_at or o.created_at))
            for o in await storage.get_recent_closing_fills(limit=50)
        ]
        if not outcomes:  # pre-§7.46 history: entry decisions carry one outcome each
            outcomes = [
                (float(d.realized_pnl or 0.0), _as_utc(d.timestamp))
                for d in await storage.get_closed_decisions(limit=50)
                if d.action == "buy"
            ]
        streak = 0
        newest_loss_ts: datetime | None = None
        for pnl, ts in outcomes:  # newest first
            if pnl < 0.0:
                streak += 1
                newest_loss_ts = newest_loss_ts or ts
            else:
                break
        cooldown_until = None
        threshold = risk_engine.settings.consecutive_losses_threshold
        if streak >= threshold and newest_loss_ts is not None:
            cooldown_until = newest_loss_ts + timedelta(
                minutes=risk_engine.settings.consecutive_losses_cooldown_minutes
            )
        if streak or cooldown_until is not None:
            risk_engine.restore_loss_streak(streak, cooldown_until)
            logger.info(
                "loss-streak state rehydrated",
                consecutive_losses=streak,
                cooldown_until=cooldown_until.isoformat() if cooldown_until else None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to rehydrate loss-streak state", error=str(exc))


async def rehydrate_from_storage(risk_engine: RiskEngine, executor: Any, storage: Storage) -> None:
    """One startup call covering every rehydratable component (see module docstring)."""
    await rehydrate_paper_executor(executor, storage)
    await rehydrate_venue_executor(executor, storage)
    await rehydrate_risk_engine(risk_engine, storage)
