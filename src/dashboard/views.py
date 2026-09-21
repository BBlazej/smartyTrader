"""Pure view-model builders for the dashboard (§7.15 P3).

These are deliberately free of HTTP and DB I/O: they take already-fetched storage
rows (``LLMDecisionRow`` / ``PortfolioSnapshotRow`` ORM objects, or anything exposing
the same attributes) and return plain dicts/Pydantic models the templates render.
Keeping them pure makes the win-rate math, chart-array shaping and position parsing
unit-testable without spinning up FastAPI or SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from ..core.models import Position

logger = structlog.get_logger(__name__)

# Confidence histogram edges (5 equal-width buckets across [0, 1]).
_CONFIDENCE_BINS: tuple[tuple[float, float], ...] = (
    (0.0, 0.2),
    (0.2, 0.4),
    (0.4, 0.6),
    (0.6, 0.8),
    (0.8, 1.0),
)


def agent_status(
    *,
    enabled: bool,
    state: str,
    last_cycle_at: datetime | None,
    interval_minutes: int = 5,
    now: datetime | None = None,
) -> str:
    """Effective on-screen status of an agent: ``disabled`` / ``offline`` / ``paused`` / ``running``.

    The ``agent_control.state`` latch records *intent*, not process liveness — a stopped
    agent keeps its last latch value (``running``, or even ``paused``) forever, so
    freshness is checked first: no heartbeat, or one older than twice the configured
    interval (floored at 10 min, plus a 5-min grace), is ``offline`` regardless of the
    latch. Agents stamp heartbeats on normal, market-hours-skipped *and* paused cycles,
    so a stale beat unambiguously means the process is gone.

    Timestamps are naive UTC, as stored by SQLite (see :func:`portfolio_chart`).
    """
    if not enabled:
        return "disabled"
    if last_cycle_at is None:
        return "offline"  # never ticked — a paused latch needs at least one beat to mean anything
    if now is None:
        now = datetime.now(UTC).replace(tzinfo=None)
    stale_after_seconds = max(2 * interval_minutes, 10) * 60 + 300
    age_seconds = (now - last_cycle_at).total_seconds()
    if age_seconds > stale_after_seconds:
        return "offline"  # beats stop when the process does — even with a paused latch
    return "paused" if state == "paused" else "running"


def tail_lines(content: str, max_lines: int = 400) -> str:
    """Keep only the last ``max_lines`` lines of a (possibly huge) log body."""
    lines = content.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def parse_positions(snapshot: Any) -> list[Position]:
    """Parse the ``positions_json`` of a portfolio snapshot into ``Position`` models.

    Tolerates a missing/blank/corrupt blob by returning an empty list — the positions
    panel must degrade to "no open positions", never crash the page.
    """
    import json

    raw = getattr(snapshot, "positions_json", None) if snapshot is not None else None
    if not raw or not isinstance(raw, str):
        return []
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    positions: list[Position] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            positions.append(Position(**entry))
        except Exception as exc:  # noqa: BLE001 - a bad position must not sink the page
            logger.warning("dashboard skipped malformed position entry", error=str(exc))
            continue
    return positions


def portfolio_chart(history: list[Any], limit: int | None = None) -> dict[str, list[Any]]:
    """Shape portfolio snapshots into columnar arrays for uPlot (oldest → newest).

    ``history`` arrives newest-first (as ``get_portfolio_history`` returns it); we
    reverse it so the chart reads left-to-right in time. Timestamps are rendered as
    ISO strings (SQLite stores naive UTC) plus epoch-seconds for the x-axis.
    """
    rows = list(reversed(history))
    if limit is not None and limit > 0:
        rows = rows[-limit:]
    ts_iso: list[str] = []
    ts_epoch: list[float] = []
    total_value: list[float] = []
    cash: list[float] = []
    unrealized: list[float] = []
    for row in rows:
        stamp = getattr(row, "timestamp", None)
        if stamp is not None:
            ts_iso.append(stamp.isoformat())
            # SQLite stores naive UTC; interpret it as UTC to get true epoch seconds.
            ts_epoch.append(stamp.replace(tzinfo=UTC).timestamp())
        else:  # pragma: no cover - snapshots always carry a timestamp
            ts_iso.append("")
            ts_epoch.append(0.0)
        total_value.append(float(getattr(row, "total_value", 0.0)))
        cash.append(float(getattr(row, "cash", 0.0)))
        unrealized.append(float(getattr(row, "unrealized_pnl", 0.0)))
    return {
        "timestamp": ts_iso,
        "x": ts_epoch,
        "total_value": total_value,
        "cash": cash,
        "unrealized_pnl": unrealized,
    }


def decision_stats(rows: list[Any]) -> dict[str, Any]:
    """Compute win-rate / confidence / action metrics over a set of decision rows.

    A *win* is a closed trade (``realized_pnl`` set, net of fees) with positive PnL;
    the win rate is ``wins / closed`` and is ``None`` when nothing has closed yet
    (rather than a misleading 0%). LLM-unavailable fallback HOLDs are counted but
    excluded from the actionable stats so they don't dilute confidence/win-rate.
    """
    total = len(rows)
    real = [r for r in rows if not getattr(r, "is_fallback", False)]
    fallback = total - len(real)

    buys = sum(1 for r in real if getattr(r, "action", None) == "buy")
    sells = sum(1 for r in real if getattr(r, "action", None) == "sell")
    holds = sum(1 for r in real if getattr(r, "action", None) == "hold")
    approved = sum(1 for r in real if getattr(r, "risk_verdict", None) == "approved")
    rejected = sum(1 for r in real if getattr(r, "risk_verdict", None) == "rejected")

    closed = [r for r in rows if getattr(r, "realized_pnl", None) is not None]
    wins = sum(1 for r in closed if (getattr(r, "realized_pnl", 0.0) or 0.0) > 0)
    losses = len(closed) - wins
    win_rate = (wins / len(closed)) if closed else None
    realized_total = float(sum((getattr(r, "realized_pnl", 0.0) or 0.0) for r in closed))

    actionable = [r for r in real if getattr(r, "action", None) in ("buy", "sell")]
    confs = [float(getattr(r, "confidence", 0.0) or 0.0) for r in actionable]
    avg_confidence = (sum(confs) / len(confs)) if confs else None

    buckets: list[dict[str, Any]] = []
    for lo, hi in _CONFIDENCE_BINS:
        # A confidence exactly on a bin's upper edge lands in the higher bucket,
        # except 1.0 which belongs in the top bucket.
        count = sum(1 for c in confs if (lo <= c < hi) or (hi >= 1.0 and c == 1.0 and lo >= 0.8))
        buckets.append({"label": f"{lo:.1f}–{hi:.1f}", "count": count})

    return {
        "total": total,
        "fallback": fallback,
        "buys": buys,
        "sells": sells,
        "holds": holds,
        "approved": approved,
        "rejected": rejected,
        "closed": len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "realized_total": realized_total,
        "avg_confidence": avg_confidence,
        "confidence_buckets": buckets,
    }
