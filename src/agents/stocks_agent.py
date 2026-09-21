"""Stocks agent — orchestrates the full decision cycle for configured symbols.

Shares everything with :class:`BaseTradingAgent` (§7.13); this subclass adds the one
genuinely market-specific concern: the **market-hours guard** (WSE 09:00–16:30 by
default, timezone-local, weekend/holiday aware, wrap-around windows supported). A cycle
requested while the exchange is closed is skipped (logged with the reason, no decision
recorded) rather than trading on stale off-hours data.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from ..core.decision_pipeline import DecisionPipeline
from ..core.llm_client import LLMClient
from ..core.risk_engine import RiskEngine
from ..core.storage import Storage
from ..monitoring.alerts import AlertManager
from .base_agent import BaseTradingAgent

# Default Warsaw Stock Exchange trading hours (local wall-clock).
DEFAULT_MARKET_HOURS = "09:00-16:30"
# The exchange the default hours assume. The market-hours window is a *local*
# wall-clock range, so ``now`` must be rendered in this zone before comparison —
# otherwise a UTC host runs the guard 1–2h off (CET/CEST).
DEFAULT_MARKET_TIMEZONE = "Europe/Warsaw"


def parse_market_hours(spec: str) -> tuple[time, time]:
    """Parse a ``"HH:MM-HH:MM"`` spec into a (start, end) pair of ``time``.

    A bare ``"HH:MM"`` (no range) is treated as a no-op window: the market is always
    considered open, so the guard never blocks a cycle.
    """
    spec = spec.strip()
    if "-" not in spec:
        return time.min, time.max
    start_s, end_s = spec.split("-", 1)
    return _parse_time(start_s.strip()), _parse_time(end_s.strip())


def _parse_time(value: str) -> time:
    hours, minutes = value.split(":", 1)
    return time(int(hours), int(minutes))


def parse_holidays(holidays: Iterable[str] | None) -> set[date]:
    """Parse ISO date strings (``"YYYY-MM-DD"``) into a set of ``date``.

    Raises :class:`ValueError` on a malformed entry: a holiday-config typo must fail
    loudly at startup rather than silently disable the guard mid-run.
    """
    parsed: set[date] = set()
    for raw in holidays or []:
        try:
            parsed.add(date.fromisoformat(raw.strip()))
        except ValueError as exc:
            raise ValueError(
                f"invalid market_holidays entry {raw!r} (expected YYYY-MM-DD)"
            ) from exc
    return parsed


def market_closed_reason(
    now: datetime, market_hours: str, holidays: set[date] | None = None
) -> str | None:
    """Return why the market is closed at ``now``, or ``None`` when it is open.

    Three checks, all on the **local wall clock** of ``now`` (the configured window
    is expected to be in the exchange's zone — for the WSE that is Europe/Warsaw):

    1. **Weekend:** Saturday/Sunday are always closed when a real window is
       configured — time-of-day alone used to let a Saturday 10:00 tick run on
       Friday's stale daily candles.
    2. **Holiday:** any date in ``holidays`` (config-driven, see
       ``stocks_agent.market_holidays``) is closed.
    3. **Trading window:** an overnight window (``start > end``, e.g.
       ``"22:00-08:00"``) wraps across midnight instead of producing the old
       never-true comparison that silently kept the agent from ever running.

    A no-op window (bare ``"HH:MM"`` spec, e.g. ``"24h"``) disables the whole
    guard — always open, weekends included.
    """
    start, end = parse_market_hours(market_hours)
    if start == time.min and end == time.max:
        return None
    if now.weekday() >= 5:
        return "weekend"
    if holidays and now.date() in holidays:
        return "holiday"
    t = now.time()
    if start > end:  # overnight window: open from start through midnight, then until end
        return None if (t >= start or t <= end) else "outside trading window"
    return None if start <= t <= end else "outside trading window"


def is_market_open(now: datetime, market_hours: str, holidays: set[date] | None = None) -> bool:
    """Return ``True`` when the market is open at ``now``.

    Thin wrapper over :func:`market_closed_reason` — see there for the weekend,
    holiday and wrap-around semantics.
    """
    return market_closed_reason(now, market_hours, holidays) is None


class StocksAgent(BaseTradingAgent):
    """Runs decision cycles for a set of stock symbols, gated by exchange hours.

    If the exchange is closed — weekend, configured holiday, or outside the trading
    window — the cycle is skipped (no decision recorded) rather than trading on stale
    data. The guard evaluates in the market's local zone (see :meth:`_local_now`), so
    a UTC host runs the WSE window at the correct local hours.
    """

    def __init__(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        llm_client: LLMClient,
        symbols: list[str],
        timeframe: str = "1d",
        market_hours: str = DEFAULT_MARKET_HOURS,
        market_timezone: str = DEFAULT_MARKET_TIMEZONE,
        market_holidays: Iterable[str] | None = None,
        alerts: AlertManager | None = None,
    ) -> None:
        super().__init__(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=llm_client,
            symbols=symbols,
            timeframe=timeframe,
            # Control-plane key must match the runner/dashboard/control-API name
            # ("stocks") — see nightly_finds #16.
            component="stocks",
            alerts=alerts,
        )
        self._market_hours = market_hours
        self._market_timezone = market_timezone
        # Validated eagerly: a malformed holiday raises here (startup), not per cycle.
        self._holidays = parse_holidays(market_holidays)

    # ── Market-hours guard ────────────────────────────────────

    def _start_log_fields(self) -> dict[str, object]:
        return {"market_hours": self._market_hours}

    def _skip_cycle_reason(self) -> str | None:
        """Weekend / holiday / outside-window reason, or ``None`` when tradable."""
        return market_closed_reason(self._local_now(), self._market_hours, self._holidays)

    def _local_now(self) -> datetime:
        """Current time in the market's local zone (falls back to UTC).

        The market-hours window is a local wall-clock range, so the guard must be
        evaluated against the exchange's zone, not the host's. An unknown zone name
        degrades gracefully to UTC rather than crashing the cycle.
        """
        try:
            return datetime.now(UTC).astimezone(ZoneInfo(self._market_timezone))
        except Exception:  # noqa: BLE001
            return datetime.now(UTC)
