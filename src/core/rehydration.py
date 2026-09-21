"""Rehydrate in-memory trading state from SQLite at startup (§7.7).

``PaperExecutor`` keeps its book (cash + positions) and ``RiskEngine`` its trackers
(daily-loss baseline, losing streak, cooldown) only in memory — while decisions,
orders and portfolio snapshots persist. Without this module every restart silently:

* reset paper cash to ``execution.initial_cash`` and dropped open positions
  (persisted decisions then referenced trades that no longer existed), and
* zeroed the daily-loss baseline and the losing-streak/cooldown state —
  weakening every stateful guard exactly when it matters.

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

from .models import Position
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
        load(cash=float(row.cash), positions=positions)
    except Exception as exc:  # noqa: BLE001 — startup must not die on a bad row
        logger.warning("failed to rehydrate paper portfolio; starting fresh", error=str(exc))
        return False
    logger.info(
        "paper portfolio rehydrated from storage",
        cash=float(row.cash),
        positions=len(positions),
    )
    return True


async def rehydrate_risk_engine(risk_engine: RiskEngine, storage: Storage) -> None:
    """Rebuild the daily-loss baseline and the losing-streak/cooldown state.

    * Baseline ← ``total_value`` of today's earliest portfolio snapshot (absent
      → left unset so it seeds lazily from the first fresh reading).
    * Losing streak ← trailing run of *closed* decisions (newest first) with a
      negative realized PnL; at the configured threshold
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
        closed = await storage.get_closed_decisions(limit=50)
        streak = 0
        newest_loss_ts: datetime | None = None
        for row in closed:  # newest first
            if (row.realized_pnl or 0.0) < 0.0:
                streak += 1
                newest_loss_ts = newest_loss_ts or _as_utc(row.timestamp)
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
    await rehydrate_risk_engine(risk_engine, storage)
