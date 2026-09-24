"""Shared trading-agent implementation (§7.13).

``CryptoAgent`` and ``StocksAgent`` were ~90% identical — cycle loop, post-processing
(daily-value update, order persistence, realized-PnL backfill), portfolio snapshot,
alerts, lifecycle. Only the market-hours guard is genuinely market-specific, so it now
lives in the subclass via the :meth:`BaseTradingAgent._skip_cycle_reason` hook and every
other fix lands once.

The agent does not place orders directly — execution always flows through the shared
pipeline + risk gate.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from ..core.decision_pipeline import DecisionPipeline, PipelineResult
from ..core.llm_client import LLMClient
from ..core.risk_engine import RiskEngine
from ..core.storage import AgentControlRow, Storage
from ..monitoring.alerts import AlertManager


class BaseTradingAgent:
    """Runs decision cycles for a set of symbols.

    Post-order persistence is fail-soft *and* lossless (§7.44): a storage error after
    a fill never aborts the cycle or skips the heartbeat, order rows are retried and
    dumped to an audit log line if they still can't be written, and reconciled venue
    status changes are only confirmed to the executor once they are persisted.

    The heavy lifting (data → indicators → LLM → risk → execute) is delegated to
    the shared :class:`DecisionPipeline`. This class adds the per-cycle concerns:
    updating the daily-loss baseline, persisting the outcome for audit +
    backtesting, attributing realized PnL (§7.8), and raising alerts.

    Market-specific subclasses hook in via:

    * :meth:`_skip_cycle_reason` — return a truthy reason to skip the whole cycle
      (e.g. the stocks market-hours guard); ``None`` means proceed.
    * :meth:`_start_log_fields` — extra fields for the startup log line.
    """

    def __init__(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        llm_client: LLMClient,
        symbols: list[str],
        timeframe: str,
        component: str,
        alerts: AlertManager | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._storage = storage
        self._risk_engine = risk_engine
        self._llm_client = llm_client
        self._symbols = symbols
        self._timeframe = timeframe
        self._alerts = alerts or AlertManager()
        self._logger = structlog.get_logger().bind(component=component)
        self._running = False
        # Control plane (§7.15): the component name keys the ``agent_control`` row;
        # the runner injects an applier closure over its own Settings/pipeline.
        self._control_agent = component
        self._overrides_applier: Callable[[str], None] | None = None
        # §7.44: back-off between order-row write attempts (tests set zeros).
        self._persist_retry_delays: tuple[float, ...] = (0.2, 1.0)

    def set_symbols(self, symbols: list[str]) -> None:
        """Replace the traded symbol list (safe config override, §7.15)."""
        if symbols and symbols != self._symbols:
            self._logger.info(
                "symbol list updated by config override", old=self._symbols, new=symbols
            )
            self._symbols = symbols

    def set_control_overrides_applier(self, applier: Callable[[str], None] | None) -> None:
        """Install a hook applying stored safe-config overrides (raw JSON) each cycle."""
        self._overrides_applier = applier

    # ── Market-specific hooks ─────────────────────────────────

    def _skip_cycle_reason(self) -> str | None:
        """Return a reason to skip the entire cycle, or ``None`` to proceed."""
        return None

    def _start_log_fields(self) -> dict[str, object]:
        """Extra fields attached to the *agent started* log line."""
        return {}

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._logger.info(
            "agent started",
            symbols=self._symbols,
            timeframe=self._timeframe,
            **self._start_log_fields(),  # type: ignore[arg-type]
        )

    async def stop(self) -> None:
        self._running = False
        self._logger.info("agent stopped")

    @property
    def running(self) -> bool:
        return self._running

    async def shutdown(self) -> None:
        """Stop the agent and release the LLM client + alert channel connections."""
        await self.stop()
        await self._llm_client.close()
        close_alerts = getattr(self._alerts, "close", None)
        if callable(close_alerts):
            await close_alerts()

    # ── Cycle ─────────────────────────────────────────────────

    async def run_cycle(self) -> list[PipelineResult]:
        """Run one decision cycle across all configured symbols.

        A cycle is a bounded, finite operation (one pipeline run per symbol) and
        always completes, even when called directly (e.g. a single manual cycle).
        A truthy :meth:`_skip_cycle_reason` skips everything (no decisions recorded).
        """
        # Control plane first (§7.15): pause/close-all/config overrides are read from
        # the DB every cycle (cheap), *before* any market-hours skip so a close-all
        # still executes while the market window is closed.
        control = await self._read_control()
        if control is not None:
            paused_reason = await self._handle_control(control)
            if paused_reason is not None:
                return []

        skip = self._skip_cycle_reason()
        if skip is not None:
            self._logger.info("cycle skipped (market closed)", reason=skip)
            # Still a heartbeat: the process is alive and ticking, so the dashboard's
            # heartbeat-derived liveness (offline when stale) must not false-alarm
            # during quiet market windows.
            await self._record_health(None)
            return []

        # Venue orders left ``open`` in a previous cycle get one status poll
        # per cycle before anything else reads the book (§7.28).
        await self._reconcile_orders()

        self._logger.info("cycle start", symbols=self._symbols)
        results: list[PipelineResult] = []
        cycle_error: str | None = None
        for symbol in self._symbols:
            try:
                result = await self._pipeline.run(symbol=symbol, timeframe=self._timeframe)
            except Exception as exc:  # noqa: BLE001
                self._logger.error("pipeline run failed", symbol=symbol, error=str(exc))
                cycle_error = f"{symbol}: {exc}"
                continue
            if result.error is not None:
                cycle_error = f"{symbol}: {result.error}"
            elif result.signal is not None and result.signal.is_fallback:
                # An LLM outage is not a quiet HOLD (§7.51): surface it on the
                # dashboard heartbeat and as an alert instead of reading "healthy".
                cycle_error = (
                    f"{symbol}: LLM unavailable — fallback HOLD ({result.signal.reasoning})"
                )
            try:
                await self._post_process(symbol, result)
            except Exception as exc:  # noqa: BLE001 - last line of defense (§7.44)
                self._logger.error("post-processing failed", symbol=symbol, error=str(exc))
                cycle_error = f"{symbol}: post-processing failed: {exc}"
            results.append(result)
        self._logger.info("cycle end", executed=sum(1 for r in results if r.executed))
        await self._record_health(cycle_error)
        return results

    # ── Control plane (§7.15) ──────────────────────────

    async def _read_control(self) -> AgentControlRow | None:
        """Fetch this agent's control row; ``None`` when absent or the read fails.

        Fail-soft in the *trading-safe* direction: a broken control read must not
        halt cycles, and never fabricates a row — defaults are plain running/no-latch.
        """
        try:
            return await self._storage.get_agent_control(self._control_agent)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("control-plane read failed; running with defaults", error=str(exc))
            return None

    async def _handle_control(self, control: AgentControlRow) -> str | None:
        """Carry out stored control intent; returns a reason to skip the rest of the cycle.

        Order matters: overrides first (the cycle should run under them), then
        close-all (must execute even while paused), then the pause check itself.
        Strict ``is True`` / equality checks — an un-configured or stubbed row must
        never accidentally trigger actions.
        """
        raw_overrides = getattr(control, "config_override_json", None)
        if raw_overrides:
            if self._overrides_applier is not None:
                try:
                    self._overrides_applier(raw_overrides)
                except Exception as exc:  # noqa: BLE001 - bad override must not kill the cycle
                    self._logger.warning(
                        "config overrides rejected; keeping previous config", error=str(exc)
                    )
            else:
                self._logger.debug("stored config overrides ignored (no applier installed)")

        if getattr(control, "close_all_requested", False) is True:
            await self._close_all_positions()
            try:  # clear the latch once attempted — a failed close alerts, it must not loop
                await self._storage.request_close_all(self._control_agent, requested=False)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("could not clear close-all latch", error=str(exc))

        if getattr(control, "state", "running") == "paused":
            self._logger.info("cycle skipped (paused via control plane)")
            # Heartbeat while paused too: a stale beat then unambiguously means the
            # process is gone, not merely paused (drives §7.24 start/stop safety).
            await self._record_health(None)
            return "paused"
        return None

    async def _close_all_positions(self) -> None:
        """Close every open position through the pipeline — no LLM, no risk gate."""
        try:
            closed = await self._pipeline.close_all_positions()
        except Exception as exc:  # noqa: BLE001
            self._logger.error("close-all failed", error=str(exc))
            await self._alerts.send("error", f"close-all failed: {exc}", severity="error")
            return
        for symbol, order in closed:
            result = PipelineResult(
                symbol=symbol,
                order_result=order,
                exit_reason="close_all",
            )
            await self._post_process(symbol, result)
        self._logger.info("close-all executed", closed=len(closed))

    async def _record_health(self, last_error: str | None) -> None:
        """Heartbeat for the dashboard (``last_cycle_at`` / ``last_error``). Fail-soft."""
        try:
            await self._storage.record_cycle_health(self._control_agent, last_error=last_error)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("health heartbeat failed", error=str(exc))

    async def _reconcile_orders(self) -> None:
        """Poll + persist status transitions of venue orders left pending (§7.28).

        Uses the executor's optional ``reconcile_open_orders()`` hook (Kraken;
        paper orders fill instantly and XTB polls internally to a terminal
        status, so those executors simply have no hook). Fail-soft: a broken
        poll must never halt the cycle. Fills realized here carry their
        entry-decision attribution back onto the decision rows (§7.8).
        """
        reconcile = getattr(self._pipeline.executor, "reconcile_open_orders", None)
        if not callable(reconcile):
            return
        try:
            updates = await reconcile()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("order reconciliation failed", error=str(exc))
            return
        if not updates:
            return
        executor = self._pipeline.executor
        confirm = getattr(executor, "confirm_reconciled", None)
        changed = False
        for order in updates:
            filled_at = order.filled_at
            if order.status == "filled" and filled_at is None:
                # Venue reported no timestamp — stamp now rather than lose the record.
                filled_at = datetime.now(UTC)
            try:
                found = await self._storage.update_order_status(
                    order.order_id,
                    order.status,
                    price=order.price,
                    filled_at=filled_at,
                    realized_pnl=order.realized_pnl if order.status == "filled" else None,
                )
                if not found:
                    # The original row was lost (e.g. a failed write at placement):
                    # re-create it from the venue's answer rather than drop the fill.
                    decision_of = getattr(executor, "pending_decision_id", None)
                    await self._storage.save_order(
                        order_id=order.order_id,
                        symbol=order.symbol,
                        side=order.side.value,
                        quantity=order.quantity,
                        price=order.price,
                        status=order.status,
                        decision_id=decision_of(order.order_id) if callable(decision_of) else None,
                        filled_at=filled_at if order.status == "filled" else None,
                        realized_pnl=order.realized_pnl if order.status == "filled" else None,
                    )
            except Exception as exc:  # noqa: BLE001
                # Not confirmed → the executor re-delivers this transition next cycle.
                self._logger.warning(
                    "failed to persist reconciled order; will retry next cycle",
                    order_id=order.order_id,
                    error=str(exc),
                )
                continue
            for entry in order.closed_entries:
                if entry.entry_decision_id is not None:
                    await self._storage.add_realized_pnl(entry.entry_decision_id, entry.pnl)
            if order.status == "filled" and order.realized_pnl is not None:
                # A late closing fill counts toward the loss streak like any other (§7.46).
                self._risk_engine.record_outcome(was_profitable=order.realized_pnl >= 0)
            if callable(confirm):
                confirm(order.order_id)
            self._logger.info(
                "order reconciled",
                order_id=order.order_id,
                status=order.status,
                symbol=order.symbol,
            )
            changed = True
        if changed:
            # Cash/positions moved at the venue — keep the stored book current.
            await self._persist_portfolio()

    async def _post_process(self, symbol: str, result: PipelineResult) -> None:
        """Update risk tracking and persist the decision / order / portfolio.

        Every step is fail-soft (§7.44): the order has already executed, so a storage
        hiccup here must neither abort the cycle nor lose the record.
        """
        # Keep the daily-loss baseline fresh so the -2% rule stays meaningful.
        try:
            portfolio = await self._pipeline.get_portfolio_state()
            self._risk_engine.update_daily_value(portfolio.total_value)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("failed to update daily value", symbol=symbol, error=str(exc))

        # The pipeline persists the decision itself (right after the risk gate)
        # so an order can link back to its decision row; we just carry the id.
        decision_id = result.decision_id

        if result.order_result is not None:
            await self._persist_order(result, decision_id)
            try:
                # Backfill the net realized PnL onto the decision so the LLM can see
                # the *outcome* of each past action (the "learn from its track
                # record" loop). A win/loss is only knowable once a position closes.
                if (
                    decision_id is not None
                    and result.order_result.status == "filled"
                    and result.order_result.realized_pnl is not None
                ):
                    await self._storage.set_realized_pnl(
                        decision_id, result.order_result.realized_pnl
                    )

                # Attribute the closing sell's PnL back to the *entry* decisions that
                # opened the consumed lots (§7.8 — FIFO tracker's closed_entries).
                for entry in result.order_result.closed_entries:
                    if entry.entry_decision_id is not None:
                        await self._storage.add_realized_pnl(entry.entry_decision_id, entry.pnl)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "failed to backfill realized pnl", symbol=symbol, error=str(exc)
                )

        await self._persist_portfolio()
        try:
            await self._maybe_alert(symbol, result)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("alert dispatch failed", symbol=symbol, error=str(exc))

    async def _maybe_alert(self, symbol: str, result: PipelineResult) -> None:
        """Raise a user-facing alert for noteworthy outcomes (never for a plain HOLD)."""
        if result.error is not None:
            await self._alerts.send("error", result.error, severity="error", symbol=symbol)
            return

        if result.signal is not None and result.signal.is_fallback:
            await self._alerts.send(
                "llm_unavailable",
                f"{symbol}: LLM unavailable, cycle fell back to HOLD — {result.signal.reasoning}",
                severity="error",
                symbol=symbol,
            )
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
        """Write the order row — retried; never raises (§7.44).

        The row is the source of the §7.25 FIFO replay, so losing it silently would
        desync book and ledger after the next restart. After the retries, the full
        order goes to an ``order_persist_failed`` audit log line plus an alert.
        """
        order = result.order_result
        assert order is not None
        filled_at = order.filled_at or datetime.now(UTC)
        row = {
            "order_id": order.order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "price": order.price,
            "status": order.status,
            "decision_id": decision_id,
            "filled_at": filled_at if order.status == "filled" else None,
            # One outcome per closing fill — the loss-streak rehydration source (§7.46).
            "realized_pnl": order.realized_pnl if order.status == "filled" else None,
        }
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, *self._persist_retry_delays), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._storage.save_order(**row)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._logger.warning(
                    "order persist attempt failed",
                    order_id=order.order_id,
                    attempt=attempt,
                    error=str(exc),
                )
                continue
            self._logger.info("order stored", order_id=order.order_id, status=order.status)
            return
        self._logger.error(
            "order_persist_failed",
            **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()},
            error=str(last_error),
        )
        try:
            await self._alerts.send(
                "error",
                f"order {order.order_id} executed but could not be stored: {last_error}",
                severity="error",
                symbol=order.symbol,
            )
        except Exception as exc:  # noqa: BLE001 - the audit line above is the record
            self._logger.warning("alert dispatch failed", error=str(exc))

    async def _persist_portfolio(self) -> None:
        try:
            portfolio = await self._pipeline.get_portfolio_state()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("failed to read portfolio for persistence", error=str(exc))
            return
        positions_json = json.dumps([p.model_dump(mode="json") for p in portfolio.positions])
        try:
            await self._storage.save_portfolio_snapshot(
                cash=portfolio.cash,
                positions_json=positions_json,
                total_value=portfolio.total_value,
                unrealized_pnl=portfolio.unrealized_pnl,
            )
        except Exception as exc:  # noqa: BLE001 - next cycle writes a fresh snapshot
            self._logger.warning("failed to persist portfolio snapshot", error=str(exc))
