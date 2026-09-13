"""Crypto agent — orchestrates the full decision cycle for configured pairs.

Owns one cycle per symbol: run the decision pipeline, update risk tracking,
and persist everything (decision, order, portfolio). The agent does not place
orders directly — execution always flows through the shared pipeline + risk gate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog

from ..core.decision_pipeline import DecisionPipeline, PipelineResult
from ..core.llm_client import LLMClient
from ..core.risk_engine import RiskEngine
from ..core.storage import Storage
from ..monitoring.alerts import AlertManager


class CryptoAgent:
    """Runs decision cycles for a set of crypto pairs.

    The heavy lifting (data → indicators → LLM → risk → execute) is delegated to
    the shared :class:`DecisionPipeline`. This agent adds the per-market concerns:
    which symbols to trade, updating the daily-loss baseline, and persisting the
    outcome for audit + backtesting.
    """

    def __init__(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        llm_client: LLMClient,
        pairs: list[str],
        timeframe: str = "1h",
        alerts: AlertManager | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._storage = storage
        self._risk_engine = risk_engine
        self._llm_client = llm_client
        self._pairs = pairs
        self._timeframe = timeframe
        self._alerts = alerts or AlertManager()
        self._logger = structlog.get_logger().bind(component="crypto_agent")
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._logger.info("crypto agent started", pairs=self._pairs, timeframe=self._timeframe)

    async def stop(self) -> None:
        self._running = False
        self._logger.info("crypto agent stopped")

    @property
    def running(self) -> bool:
        return self._running

    async def shutdown(self) -> None:
        """Stop the agent and release the LLM client connection."""
        await self.stop()
        await self._llm_client.close()

    # ── Cycle ─────────────────────────────────────────────────

    async def run_cycle(self) -> list[PipelineResult]:
        """Run one decision cycle across all configured pairs.

        A cycle is a bounded, finite operation (one pipeline run per pair) and
        always completes, even when called directly (e.g. a single manual cycle).
        """
        self._logger.info("cycle start", pairs=self._pairs)
        results: list[PipelineResult] = []
        for symbol in self._pairs:
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
