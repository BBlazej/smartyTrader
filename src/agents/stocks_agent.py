"""Stocks agent — orchestrates the full decision cycle for configured symbols.

Mirrors :class:`src.agents.crypto_agent.CryptoAgent` for the stock market: the heavy
lifting (data → indicators → LLM → risk → execute) is delegated to the shared
:class:`DecisionPipeline`. This agent adds the per-market concerns: which symbols to
trade, a market-hours guard (WSE: 09:00–16:30 by default), and persistence for audit.

Unlike crypto, the stock cycle is gated by exchange trading hours — a cycle requested
outside those hours is skipped (logged, no decision recorded) rather than trading on
stale off-hours data.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import structlog

from ..core.decision_pipeline import DecisionPipeline, PipelineResult
from ..core.llm_client import LLMClient
from ..core.risk_engine import RiskEngine
from ..core.storage import Storage
from ..monitoring.alerts import AlertManager

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


class StocksAgent:
    """Runs decision cycles for a set of stock symbols.

    The heavy lifting (data → indicators → LLM → risk → execute) is delegated to the
    shared :class:`DecisionPipeline`. This agent adds the per-market concerns: which
    symbols to trade, the market-hours guard, and persisting the outcome.
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
        self._pipeline = pipeline
        self._storage = storage
        self._risk_engine = risk_engine
        self._llm_client = llm_client
        self._symbols = symbols
        self._timeframe = timeframe
        self._market_hours = market_hours
        self._market_timezone = market_timezone
        # Validated eagerly: a malformed holiday raises here (startup), not per cycle.
        self._holidays = parse_holidays(market_holidays)
        self._alerts = alerts or AlertManager()
        self._logger = structlog.get_logger().bind(component="stocks_agent")
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._logger.info(
            "stocks agent started",
            symbols=self._symbols,
            timeframe=self._timeframe,
            market_hours=self._market_hours,
        )

    async def stop(self) -> None:
        self._running = False
        self._logger.info("stocks agent stopped")

    @property
    def running(self) -> bool:
        return self._running

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

    async def shutdown(self) -> None:
        """Stop the agent and release the LLM client connection."""
        await self.stop()
        await self._llm_client.close()

    # ── Cycle ─────────────────────────────────────────────────

    async def run_cycle(self) -> list[PipelineResult]:
        """Run one decision cycle across all configured symbols.

        A cycle is a bounded, finite operation (one pipeline run per symbol) and
        always completes. If the exchange is closed — weekend, configured holiday,
        or outside the trading window — the cycle is skipped (no decision recorded)
        rather than trading on stale data.

        The guard is evaluated in the market's local zone (see ``_local_now``), so
        a UTC host runs the WSE window at the correct local hours.
        """
        closed_reason = market_closed_reason(self._local_now(), self._market_hours, self._holidays)
        if closed_reason is not None:
            self._logger.info(
                "cycle skipped (market closed)",
                reason=closed_reason,
                market_hours=self._market_hours,
            )
            return []

        self._logger.info("cycle start", symbols=self._symbols)
        results: list[PipelineResult] = []
        for symbol in self._symbols:
            try:
                result = await self._pipeline.run(symbol=symbol, timeframe=self._timeframe)
            except Exception as exc:  # noqa: BLE001
                self._logger.error("pipeline run failed", symbol=symbol, error=str(exc))
                continue
            await self._post_process(symbol, result)
            results.append(result)
        self._logger.info("cycle end", executed=sum(1 for r in results if r.executed))
        return results

    async def _post_process(self, symbol: str, result: PipelineResult) -> None:
        """Update risk tracking and persist the decision / order / portfolio."""
        # Keep the daily-loss baseline fresh so the -2% rule stays meaningful.
        try:
            portfolio = await self._pipeline._get_portfolio_state()
            self._risk_engine.update_daily_value(portfolio.total_value)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("failed to update daily value", symbol=symbol, error=str(exc))

        # The pipeline persists the decision itself (right after the risk gate)
        # so an order can link back to its decision row; we just carry the id.
        decision_id = result.decision_id

        if result.order_result is not None:
            await self._persist_order(result, decision_id)
            # Backfill the net realized PnL onto the decision so the LLM can see
            # the *outcome* of each past action (the "learn from its track
            # record" loop). A win/loss is only knowable once a position closes.
            if (
                decision_id is not None
                and result.order_result.status == "filled"
                and result.order_result.realized_pnl is not None
            ):
                await self._storage.set_realized_pnl(decision_id, result.order_result.realized_pnl)

            # Attribute the closing sell's PnL back to the *entry* decisions that
            # opened the consumed lots (§7.8 — FIFO tracker's closed_entries).
            for entry in result.order_result.closed_entries:
                if entry.entry_decision_id is not None:
                    await self._storage.add_realized_pnl(entry.entry_decision_id, entry.pnl)

        await self._persist_portfolio()
        await self._maybe_alert(symbol, result)

    async def _maybe_alert(self, symbol: str, result: PipelineResult) -> None:
        """Raise a user-facing alert for noteworthy outcomes (never for a plain HOLD)."""
        if result.error is not None:
            await self._alerts.send("error", result.error, severity="error", symbol=symbol)
            return

        if result.risk_result is not None and result.risk_result.verdict.value == "rejected":
            await self._alerts.send(
                "risk_rejected",
                result.risk_result.reason or "rejected by risk engine",
                severity="warning",
                symbol=symbol,
            )
            return

        if result.auto_exit:
            order = result.order_result
            closed = f"{order.quantity} {symbol} ({order.status})" if order else symbol
            await self._alerts.send(
                "exit_level",
                f"{result.exit_reason} breached — attempted auto-close of {closed}",
                severity="warning",
                symbol=symbol,
            )
            return

        if result.executed:
            assert result.order_result is not None
            await self._alerts.send(
                "order_filled",
                f"{result.order_result.side.value.upper()} {result.order_result.quantity} {symbol} @ {result.order_result.price}",
                severity="info",
                symbol=symbol,
            )

    # ── Persistence ───────────────────────────────────────────

    async def _persist_order(self, result: PipelineResult, decision_id: int | None = None) -> None:
        order = result.order_result
        assert order is not None
        filled_at = order.filled_at or datetime.now(UTC)
        await self._storage.save_order(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            price=order.price,
            status=order.status,
            decision_id=decision_id,
            filled_at=filled_at if order.status == "filled" else None,
        )
        self._logger.info("order stored", order_id=order.order_id, status=order.status)

    async def _persist_portfolio(self) -> None:
        try:
            portfolio = await self._pipeline._get_portfolio_state()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("failed to read portfolio for persistence", error=str(exc))
            return
        positions_json = json.dumps([p.model_dump(mode="json") for p in portfolio.positions])
        await self._storage.save_portfolio_snapshot(
            cash=portfolio.cash,
            positions_json=positions_json,
            total_value=portfolio.total_value,
            unrealized_pnl=portfolio.unrealized_pnl,
        )
