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
from datetime import UTC, datetime, time

import structlog

from ..core.decision_pipeline import DecisionPipeline, PipelineResult
from ..core.llm_client import LLMClient
from ..core.risk_engine import RiskEngine
from ..core.storage import Storage
from ..monitoring.alerts import AlertManager

# Default Warsaw Stock Exchange trading hours (local wall-clock).
DEFAULT_MARKET_HOURS = "09:00-16:30"


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


def is_market_open(now: datetime, market_hours: str) -> bool:
    """Return ``True`` when ``now`` falls within the ``market_hours`` window.

    The comparison is on the **local wall clock** of ``now``. The configured window
    is therefore expected to be in the same local time zone as ``now`` (for the WSE
    that is Europe/Warsaw). A no-op window (bare ``"HH:MM"``) is always open.
    """
    start, end = parse_market_hours(market_hours)
    if start == time.min and end == time.max:
        return True
    return start <= now.time() <= end


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
        alerts: AlertManager | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._storage = storage
        self._risk_engine = risk_engine
        self._llm_client = llm_client
        self._symbols = symbols
        self._timeframe = timeframe
        self._market_hours = market_hours
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

    async def shutdown(self) -> None:
        """Stop the agent and release the LLM client connection."""
        await self.stop()
        await self._llm_client.close()

    # ── Cycle ─────────────────────────────────────────────────

    async def run_cycle(self) -> list[PipelineResult]:
        """Run one decision cycle across all configured symbols.

        A cycle is a bounded, finite operation (one pipeline run per symbol) and
        always completes. If the exchange is closed for the configured window, the
        cycle is skipped (no decision recorded) rather than trading on stale data.
        """
        if not is_market_open(datetime.now(UTC), self._market_hours):
            self._logger.info("cycle skipped (market closed)", market_hours=self._market_hours)
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

        decision_id = await self._persist_decision(symbol, result) if result.signal else None

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

        if result.executed:
            assert result.order_result is not None
            await self._alerts.send(
                "order_filled",
                f"{result.order_result.side.value.upper()} {result.order_result.quantity} {symbol} @ {result.order_result.price}",
                severity="info",
                symbol=symbol,
            )

    # ── Persistence ───────────────────────────────────────────

    async def _persist_decision(self, symbol: str, result: PipelineResult) -> int:
        signal = result.signal
        assert signal is not None
        verdict = result.risk_result.verdict.value if result.risk_result else "unknown"
        reason = result.risk_result.reason if result.risk_result else None

        decision_id = await self._storage.save_llm_decision(
            symbol=symbol,
            action=signal.action.value,
            confidence=signal.confidence,
            reasoning=signal.reasoning,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            risk_verdict=verdict,
            risk_reason=reason,
        )

        if result.snapshot is not None:
            candles_json = json.dumps([c.model_dump(mode="json") for c in result.snapshot.candles])
            indicators_json = json.dumps(result.snapshot.indicators)
            await self._storage.save_market_snapshot(
                symbol=symbol,
                timeframe=result.snapshot.timeframe,
                candles_json=candles_json,
                indicators_json=indicators_json,
            )
        self._logger.info(
            "decision stored", symbol=symbol, decision_id=decision_id, action=signal.action.value
        )
        return decision_id

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
