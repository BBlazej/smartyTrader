"""Candle-timing helpers (§7.56): timeframe durations and the still-forming bar.

Venues return the *current* bar alongside closed ones — its OHLCV keeps changing until
the bar closes (partial volume, a close that is really the last trade). Indicators are
computed on **closed** bars only so they don't wobble from cycle to cycle, while the
forming bar still supplies the live price for marking and exit checks and is shown to
the LLM clearly labelled. Candle timestamps are bar *open* times (ccxt and yfinance).
Pure functions — no I/O.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

_TIMEFRAME_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86_400, "w": 604_800}


def timeframe_delta(timeframe: str) -> timedelta | None:
    """Duration of one bar for ccxt-style timeframes (``5m``, ``1h``, ``1d``, ``1w``).

    ``None`` for anything unrecognized — callers then skip forming-bar handling
    rather than guessing.
    """
    match = _TIMEFRAME_RE.match(timeframe.strip()) if timeframe else None
    if match is None:
        return None
    count, unit = int(match.group(1)), match.group(2)
    if count <= 0:
        return None
    return timedelta(seconds=count * _UNIT_SECONDS[unit])


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def split_forming(candles: list[Any], timeframe: str, now: datetime) -> tuple[list[Any], Any]:
    """Split ``candles`` into ``(closed, forming)`` — ``forming`` is ``None`` when the
    last bar has already closed (or timing is unknown, in which case all count as closed).
    """
    if not candles:
        return candles, None
    duration = timeframe_delta(timeframe)
    last = candles[-1]
    if duration is None or getattr(last, "timestamp", None) is None:
        return candles, None
    if _aware(last.timestamp) + duration > _aware(now):
        return candles[:-1], last
    return candles, None


def bar_close_time(candle: Any, timeframe: str) -> datetime | None:
    """When ``candle``'s bar closed/closes (open time + timeframe), or ``None`` if unknown."""
    duration = timeframe_delta(timeframe)
    if duration is None or getattr(candle, "timestamp", None) is None:
        return None
    return _aware(candle.timestamp) + duration
