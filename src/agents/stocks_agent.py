"""Stocks agent — orchestrates the full decision cycle for configured symbols.

Shares everything with :class:`BaseTradingAgent` (§7.13); this subclass adds the one
genuinely market-specific concern: the **market-hours guard** (NYSE 09:30–16:00
America/New_York by default — the shipped universe is US stocks, §7.68 — timezone-local,
weekend/holiday aware, wrap-around windows supported, per-exchange windows §7.66). A cycle
requested while the exchange is closed is skipped (logged with the reason, no decision
recorded) rather than trading on stale off-hours data.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from ..core.decision_pipeline import DecisionPipeline
from ..core.llm_client import LLMClient
from ..core.risk_engine import RiskEngine
from ..core.storage import Storage
from ..monitoring.alerts import AlertManager
from .base_agent import BaseTradingAgent

# Default NYSE regular trading hours (local wall-clock) — the shipped universe is US
# stocks (§7.68); the pre-§7.68 default was the Warsaw exchange's 09:00-16:30.
DEFAULT_MARKET_HOURS = "09:30-16:00"
# The exchange the default hours assume. The market-hours window is a *local*
# wall-clock range, so ``now`` must be rendered in this zone before comparison —
# otherwise a UTC host runs the guard hours off.
DEFAULT_MARKET_TIMEZONE = "America/New_York"


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
    is expected to be in the exchange's zone — for the NYSE that is America/New_York):

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


class StocksAgent(BaseTradingAgent):
    """Runs decision cycles for a set of stock symbols, gated by exchange hours.

    If the exchange is closed — weekend, configured holiday, or outside the trading
    window — the cycle is skipped (no decision recorded) rather than trading on stale
    data. The guard evaluates in the market's local zone (see :meth:`_local_now`), so
    a UTC host runs the exchange window at the correct local hours.
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
        agent_settings: Any | None = None,
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
        # §7.50 single reader: the runner passes its live ``settings.stocks_agent`` —
        # the very object the control plane mutates with safe-config overrides — so a
        # market-hours override takes effect on the next cycle instead of being lost
        # against this constructor copy. Without it (tests, standalone use) the
        # constructor value is the source of truth.
        self._agent_settings = agent_settings
        self._exchange_holidays: dict[str, set[date]] = {}

    # ── Market-hours guard ────────────────────────────────────

    @property
    def _effective_market_hours(self) -> str:
        """The live market-hours window — settings object first (§7.50)."""
        if self._agent_settings is not None:
            return getattr(self._agent_settings, "market_hours", None) or self._market_hours
        return self._market_hours

    def _start_log_fields(self) -> dict[str, object]:
        return {"market_hours": self._effective_market_hours}

    def _skip_cycle_reason(self) -> str | None:
        """Weekend / holiday / outside-window reason, or ``None`` when tradable.

        With per-exchange windows (§7.66) the whole cycle is skipped only when *every*
        window a traded symbol uses is closed; otherwise :meth:`_skip_symbol_reason`
        skips the closed ones symbol by symbol.
        """
        default_reason = market_closed_reason(
            self._local_now(), self._effective_market_hours, self._holidays
        )
        exchanges = self._exchanges()
        if not exchanges:
            return default_reason
        reasons: dict[str, str | None] = {}
        for symbol in self._symbols:
            code = self._symbol_exchange(symbol)
            if (code or "") not in reasons:
                reasons[code or ""] = (
                    self._exchange_closed_reason(exchanges[code]) if code else default_reason
                )
        if reasons and all(reason is not None for reason in reasons.values()):
            return "; ".join(f"{code or 'default'}: {reason}" for code, reason in reasons.items())
        return None

    def _skip_symbol_reason(self, symbol: str) -> str | None:
        exchanges = self._exchanges()
        if not exchanges:
            return None  # one window: the cycle-level check already decided
        code = self._symbol_exchange(symbol)
        if code is None:
            return market_closed_reason(
                self._local_now(), self._effective_market_hours, self._holidays
            )
        reason = self._exchange_closed_reason(exchanges[code])
        return f"{code}: {reason}" if reason is not None else None

    # ── Per-exchange windows (§7.66) ──────────────────────────

    def _exchanges(self) -> dict[str, Any]:
        return dict(getattr(self._agent_settings, "exchanges", None) or {})

    def _symbol_exchange(self, symbol: str) -> str | None:
        mapping = getattr(self._agent_settings, "symbol_exchanges", None) or {}
        code = mapping.get(symbol)
        return code if code in self._exchanges() else None

    def _exchange_closed_reason(self, window: Any) -> str | None:
        holidays = self._exchange_holidays.get(window.name)
        if holidays is None:
            holidays = self._exchange_holidays[window.name] = parse_holidays(window.market_holidays)
        return market_closed_reason(
            self._local_now(window.market_timezone), window.market_hours, holidays
        )

    def _local_now(self, zone: str | None = None) -> datetime:
        """Current time in the market's local zone (falls back to UTC).

        The market-hours window is a local wall-clock range, so the guard must be
        evaluated against the exchange's zone, not the host's. An unknown zone name
        degrades gracefully to UTC rather than crashing the cycle.
        """
        try:
            return datetime.now(UTC).astimezone(ZoneInfo(zone or self._market_timezone))
        except Exception:  # noqa: BLE001
            return datetime.now(UTC)
