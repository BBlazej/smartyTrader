"""Decision pipeline — orchestrates fetch → indicators → LLM → risk → execute."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

import structlog

from ..analysis.candles import bar_close_time, split_forming
from ..analysis.indicators import compute_indicators
from ..analysis.prompt_builder import DEFAULT_SYSTEM_PROMPT, build_user_prompt
from .config import RiskSettings
from .costs import CostModel
from .llm_client import LLMCallMetrics, LLMClient
from .models import (
    OHLCV,
    Action,
    BookContext,
    DecisionRecord,
    Executor,
    MarketSnapshot,
    OrderResult,
    OrderSide,
    PortfolioState,
    Position,
    PositionSide,
    RiskResult,
    RiskVerdict,
    TradeSignal,
)
from .risk_engine import RiskEngine, long_exposure
from .storage import Storage

logger = structlog.get_logger()


# ── Protocols for swappable components ────────────────────────


class MarketDataProvider(Protocol):
    """Provides market data for a symbol."""

    async def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot: ...


# ── Pipeline Result ───────────────────────────────────────────


class PipelineStep(StrEnum):
    """Pipeline stage names stamped onto structured log lines (a real Enum, §7.19)."""

    FETCH_DATA = "fetch_data"
    ENFORCE_EXIT_LEVELS = "enforce_exit_levels"
    COMPUTE_INDICATORS = "compute_indicators"
    BUILD_PROMPT = "build_prompt"
    CALL_LLM = "call_llm"
    RISK_CHECK = "risk_check"
    EXECUTE = "execute"


class PipelineResult:
    """Immutable record of a single decision-cycle run."""

    def __init__(
        self,
        symbol: str,
        signal: TradeSignal | None = None,
        risk_result: RiskResult | None = None,
        order_result: OrderResult | None = None,
        snapshot: MarketSnapshot | None = None,
        error: str | None = None,
        decision_id: int | None = None,
        auto_exit: bool = False,
        exit_reason: str | None = None,
        skip_reason: str | None = None,
    ) -> None:
        self.symbol = symbol
        self.signal = signal
        self.risk_result = risk_result
        self.order_result = order_result
        self.snapshot = snapshot
        self.error = error
        # Row id of this cycle's persisted decision (None when no storage or no
        # signal). Orders and outcome backfills link through it (§7.8).
        self.decision_id = decision_id
        # True when the cycle ended in a deterministic stop-loss/take-profit
        # close instead of an LLM decision; ``exit_reason`` says which level
        # fired (§7.9).
        self.auto_exit = auto_exit
        self.exit_reason = exit_reason
        # Set when the cycle deliberately asked no LLM question (§7.56: the latest
        # closed bar was already decided on). Marking and exit checks still ran.
        self.skip_reason = skip_reason

    @property
    def executed(self) -> bool:
        return self.order_result is not None and self.order_result.status == "filled"


# ── Pipeline ──────────────────────────────────────────────────


class DecisionPipeline:
    """Orchestrates one decision cycle for a single symbol.

    Steps:
      1. Fetch market data via provider
      2. Compute indicators (pluggable)
      3. Build prompt from snapshot + indicators
      4. Call LLM → TradeSignal
      5. Risk check → RiskResult
      6. Execute if approved
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        llm_client: LLMClient,
        risk_engine: RiskEngine,
        executor: Executor,
        system_prompt: str | None = None,
        storage: Storage | None = None,
        decision_history_limit: int = 10,
        decide_on_new_bar_only: bool = False,
    ) -> None:
        self.provider = provider
        self.llm_client = llm_client
        self.risk_engine = risk_engine
        self.executor = executor
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._storage = storage
        self._decision_history_limit = decision_history_limit
        # §7.56: cycles run more often than bars close (5-min cycles on 1h candles);
        # when set, the LLM is asked once per newly *closed* bar per symbol, while
        # every cycle still marks positions and enforces exit levels.
        self.decide_on_new_bar_only = decide_on_new_bar_only
        self._last_decision_at: dict[str, datetime] = {}

    @property
    def decision_history_limit(self) -> int:
        """How many prior decisions the prompt carries (control-plane tunable, §7.15)."""
        return self._decision_history_limit

    @decision_history_limit.setter
    def decision_history_limit(self, value: int) -> None:
        self._decision_history_limit = max(0, int(value))

    async def close_all_positions(self) -> list[tuple[str, OrderResult]]:
        """Close every open position at its current mark through the executor (§7.15).

        The dashboard's *close-all* latch lands here — mirrors §7.9 exit enforcement:
        **no LLM call and no risk gate**, because closes only reduce exposure. Orders
        are placed as market sells at each position's latest mark; a position without
        a usable price is skipped (never guessed at). Returns ``(symbol, OrderResult)``
        pairs so the caller can persist outcomes and backfill entry decisions.
        """
        closed: list[tuple[str, OrderResult]] = []
        for position in await self.executor.get_positions():
            if position.quantity <= 0:
                continue
            # §7.48: a short is closed by a covering BUY — selling it would *add* to it.
            price = position.current_price
            if not price or price <= 0:
                logger.warning(
                    "close-all skipping position without a usable mark",
                    symbol=position.symbol,
                )
                continue
            order = await self.executor.place_order(
                symbol=position.symbol,
                side=closing_side(position),
                quantity=position.quantity,
                price=price,
            )
            # A close-all fill is a real outcome for the loss streak, like every
            # other closing fill (§7.46 — it used to be skipped).
            if order.status == "filled" and order.realized_pnl is not None:
                self.risk_engine.record_outcome(was_profitable=order.realized_pnl >= 0)
            closed.append((position.symbol, order))
        return closed

    async def get_recent_decisions(
        self, symbol: str, limit: int | None = None
    ) -> list[DecisionRecord]:
        """Return this symbol's most recent prior decisions for prompt context.

        Reads the ``llm_decisions`` table (written by the agent after each cycle)
        and converts rows into lightweight :class:`DecisionRecord` objects. Returns
        an empty list when no storage is wired, the limit is zero, or the lookup
        fails — prompt context must never break a trading cycle.
        """
        count = self._decision_history_limit if limit is None else limit
        if count <= 0 or self._storage is None:
            return []

        try:
            rows = await self._storage.get_recent_decisions(symbol, limit=count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to fetch decision history", symbol=symbol, error=str(exc))
            return []

        # Which approved trades actually filled (§7.45) — so the prompt can say
        # "never traded" instead of "still open". Unknown on failure (None).
        filled_ids: set[int] | None = None
        candidates = [
            r.id for r in rows if r.action != "hold" and r.risk_verdict == "approved" and r.id
        ]
        if candidates:
            try:
                filled_ids = await self._storage.get_filled_decision_ids(candidates)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to look up decision fills", symbol=symbol, error=str(exc))

        return [
            DecisionRecord(
                action=row.action,
                confidence=row.confidence,
                reasoning=row.reasoning,
                risk_verdict=row.risk_verdict,
                risk_reason=row.risk_reason,
                realized_pnl=row.realized_pnl,
                timestamp=row.timestamp,
                filled=(row.id in filled_ids) if filled_ids is not None else None,
            )
            for row in rows
        ]

    async def run(
        self,
        symbol: str,
        timeframe: str = "1h",
    ) -> PipelineResult:
        """Execute the full decision pipeline for one symbol."""
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.FETCH_DATA)

        # Step 1 — Fetch data
        try:
            snapshot = await self.provider.fetch_snapshot(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001
            step_logger.error("fetch failed", error=str(exc))
            return PipelineResult(symbol=symbol, error=f"Fetch failed: {exc}")

        # §7.55: no candles → no price → nothing to decide or trade. Everything
        # downstream (marking, exit levels, indicators, prompt, sizing) reads the
        # candle series, and an order sent without a price becomes an *unchecked
        # market order* on the venue — past every notional cap.
        if not snapshot.candles:
            step_logger.error("provider returned zero candles; no LLM call, no order")
            return PipelineResult(symbol=symbol, error="No market data (empty candle series)")

        # Step 1b — Mark open positions to this cycle's close (paper mode).
        # Done before the risk check so the portfolio handed to the risk engine
        # — and the daily-loss baseline the agent updates afterwards — reflects
        # market moves, not a position frozen at its fill price.
        self._mark_positions(symbol, snapshot)

        # Step 1c — Deterministic exit levels (§7.9). A position that breaches its
        # stop-loss or take-profit is closed here, *without* consulting the LLM and
        # bypassing the risk gate: exits only reduce exposure, and a gate (cooldown,
        # daily-loss block) must never strand us in a losing position. The cycle
        # ends with this close — no new entry on the same symbol.
        auto_exit = await self._check_exit_levels(symbol, snapshot)
        if auto_exit is not None:
            return auto_exit

        # Step 1d — Bar timing (§7.56). The venue's last bar is usually still forming:
        # indicators use closed bars only; the forming bar keeps supplying the live
        # price (marking/exits above, prompt "current price") and is labelled.
        closed, _forming = split_forming(snapshot.candles, timeframe, snapshot.fetched_at)
        if self.decide_on_new_bar_only:
            waiting = await self._awaiting_new_bar(symbol, timeframe, closed)
            if waiting is not None:
                logger.info("no new closed bar since last decision; LLM not asked", symbol=symbol)
                return PipelineResult(symbol=symbol, snapshot=snapshot, skip_reason=waiting)

        # Step 2 — Compute indicators (closed bars only)
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.COMPUTE_INDICATORS)
        try:
            snapshot.indicators = compute_indicators(closed)
        except Exception as exc:  # noqa: BLE001
            step_logger.error("indicator computation failed", error=str(exc))
            return PipelineResult(
                symbol=symbol, snapshot=snapshot, error=f"Indicator computation failed: {exc}"
            )

        # Step 3 — Build prompt: the agent's own book for this symbol (§7.45) and its
        # recent decisions. The book is read once here and reused unchanged at the
        # risk gate — nothing trades on this agent between prompt and gate.
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.BUILD_PROMPT)
        try:
            portfolio = await self.get_portfolio_state()
        except Exception as exc:  # noqa: BLE001
            step_logger.error("portfolio read failed", error=str(exc))
            return PipelineResult(
                symbol=symbol, snapshot=snapshot, error=f"Portfolio read failed: {exc}"
            )
        prior_decisions = await self.get_recent_decisions(symbol)
        user_prompt = build_user_prompt(
            snapshot, prior_decisions, book=self._book_context(symbol, portfolio)
        )

        # Step 4 — Call LLM
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.CALL_LLM)
        signal = await self.llm_client.ask_trade_signal(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
        )
        signal.symbol = symbol  # Ensure symbol is set
        # Per-decision LLM cost profile (§7.69) — persisted onto the decision row.
        llm_metrics = getattr(self.llm_client, "last_metrics", None)
        if not isinstance(llm_metrics, LLMCallMetrics):
            llm_metrics = None  # mocks / custom clients without metrics

        # Step 5 — Risk check.
        # The proposed order size is computed *before* the gate so the risk
        # engine can reject an oversized plan (planned_notional), and reuses it
        # unchanged at execution — what the gate approved is what gets sent.
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.RISK_CHECK)
        current_price = snapshot.candles[-1].close if snapshot.candles else None
        planned_quantity: float | None = None
        planned_notional: float | None = None
        if signal.action in (Action.BUY, Action.SELL) and current_price:
            planned_quantity = self._calculate_quantity(signal, portfolio, current_price)
            planned_notional = planned_quantity * current_price
        risk_result = self.risk_engine.evaluate(
            signal, portfolio, planned_notional=planned_notional, current_price=current_price
        )

        # Persist the decision (+ market snapshot) right after the gate so every
        # outcome path — rejected, HOLD, or executed — records it, and an order
        # placed next can link back to its decision row (§7.8: entry attribution
        # needs the id *before* the fill happens).
        decision_id = await self._persist_decision(signal, risk_result, snapshot, llm_metrics)
        if not signal.is_fallback:
            # A fallback HOLD is not a decision: the next cycle retries the bar.
            decided_at = snapshot.fetched_at
            self._last_decision_at[symbol] = (
                decided_at.replace(tzinfo=UTC) if decided_at.tzinfo is None else decided_at
            )

        if risk_result.verdict == RiskVerdict.REJECTED:
            step_logger.warning(
                "risk rejected",
                reason=risk_result.reason,
                action=signal.action.value,
            )
            return PipelineResult(
                symbol=symbol,
                signal=signal,
                risk_result=risk_result,
                snapshot=snapshot,
                decision_id=decision_id,
            )

        # Step 6 — Execute (only for BUY/SELL)
        if signal.action == Action.HOLD:
            step_logger.info("holding", confidence=signal.confidence)
            return PipelineResult(
                symbol=symbol,
                signal=signal,
                risk_result=risk_result,
                snapshot=snapshot,
                decision_id=decision_id,
            )

        step_logger = logger.bind(symbol=symbol, step=PipelineStep.EXECUTE)
        try:
            order_side = OrderSide.BUY if signal.action == Action.BUY else OrderSide.SELL
            quantity = planned_quantity
            if quantity is None or not current_price:
                # §7.55 belt: a plan without a usable price never reaches the
                # executor — sized-at-stop orders with ``price=None`` used to turn
                # into unpriced market orders venue-side.
                step_logger.error("refusing to place order without a usable price")
                return PipelineResult(
                    symbol=symbol,
                    signal=signal,
                    risk_result=risk_result,
                    snapshot=snapshot,
                    decision_id=decision_id,
                    error="Refused order without price (no usable market data)",
                )
            order_result = await self.executor.place_order(
                symbol=symbol,
                side=order_side,
                quantity=quantity,
                price=current_price,
                decision_id=decision_id,
                # Carried onto the position so §7.9 can enforce them later.
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )

            # Record the realized outcome for consecutive-loss tracking. A
            # win/loss is only knowable once a position is actually closed, so
            # only orders that report a realized_pnl (e.g. a closing sell)
            # update the tracker — we never fabricate a win at fill time.
            if order_result.status == "filled" and order_result.realized_pnl is not None:
                was_profitable = order_result.realized_pnl >= 0
                self.risk_engine.record_outcome(was_profitable=was_profitable)

            step_logger.info(
                "order placed",
                status=order_result.status,
                side=order_side.value,
                quantity=quantity,
            )

            return PipelineResult(
                symbol=symbol,
                signal=signal,
                risk_result=risk_result,
                order_result=order_result,
                snapshot=snapshot,
                decision_id=decision_id,
            )
        except Exception as exc:  # noqa: BLE001
            step_logger.error("execution failed", error=str(exc))
            return PipelineResult(
                symbol=symbol,
                signal=signal,
                risk_result=risk_result,
                snapshot=snapshot,
                error=f"Execution failed: {exc}",
                decision_id=decision_id,
            )

    async def _check_exit_levels(
        self, symbol: str, snapshot: MarketSnapshot
    ) -> PipelineResult | None:
        """Close this symbol's position if the mark breached its exit levels (§7.9).

        Levels come from the entry signal and live on the executor's ``Position``
        (persisted with portfolio snapshots, so they survive restarts). Returns a
        finished :class:`PipelineResult` when an exit fired — the LLM is never
        asked, and the risk gate is deliberately skipped because closing can only
        reduce risk. Returns ``None`` when there is nothing to do.
        """
        if not self.risk_engine.settings.enforce_exit_levels or not snapshot.candles:
            return None
        mark = snapshot.candles[-1].close
        if mark <= 0:
            return None

        try:
            positions = await self.executor.get_positions()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "exit-level check skipped (positions unreadable)", symbol=symbol, error=str(exc)
            )
            return None

        # Every position in the symbol is checked, long or short (§7.48) — the first
        # breach is closed this cycle, any other on the next.
        breached = next(
            (
                (p, r)
                for p in positions
                if p.symbol == symbol and p.quantity > 0
                for r in (exit_level_breach(p, mark),)
                if r is not None
            ),
            None,
        )
        if breached is None:
            return None
        position, reason = breached

        logger.warning(
            "exit level breached",
            symbol=symbol,
            exit_reason=reason,
            mark=mark,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            quantity=position.quantity,
        )

        step_logger = logger.bind(symbol=symbol, step=PipelineStep.ENFORCE_EXIT_LEVELS)
        try:
            order_result = await self.executor.place_order(
                symbol=symbol,
                side=closing_side(position),
                quantity=position.quantity,
                price=mark,
            )
        except Exception as exc:  # noqa: BLE001
            step_logger.error("exit-level close failed", error=str(exc))
            return PipelineResult(
                symbol=symbol,
                snapshot=snapshot,
                auto_exit=True,
                exit_reason=reason,
                error=f"Exit-level close failed: {exc}",
            )

        # A closed position is a real outcome for the loss-streak tracker, same
        # rule as any other fill: only realized numbers count (§7.8).
        if order_result.status == "filled" and order_result.realized_pnl is not None:
            self.risk_engine.record_outcome(was_profitable=order_result.realized_pnl >= 0)

        step_logger.info(
            "position closed on exit level",
            exit_reason=reason,
            status=order_result.status,
            quantity=order_result.quantity,
            realized_pnl=order_result.realized_pnl,
        )
        return PipelineResult(
            symbol=symbol,
            order_result=order_result,
            snapshot=snapshot,
            auto_exit=True,
            exit_reason=reason,
        )

    async def _persist_decision(
        self,
        signal: TradeSignal,
        risk_result: RiskResult | None,
        snapshot: MarketSnapshot | None,
        llm_metrics: LLMCallMetrics | None = None,
    ) -> int | None:
        """Persist this cycle's decision (+ market snapshot); returns the row id.

        Fallback (LLM-unavailable) rows are marked ``is_fallback`` — they stay in
        the DB for audit but are excluded from prompt context (§7.8). Fail-soft:
        persistence errors never break a trading cycle.
        """
        if self._storage is None:
            return None
        verdict = risk_result.verdict.value if risk_result else "unknown"
        reason = risk_result.reason if risk_result else None
        try:
            decision_id = await self._storage.save_llm_decision(
                symbol=signal.symbol,
                action=signal.action.value,
                confidence=signal.confidence,
                reasoning=signal.reasoning,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                risk_verdict=verdict,
                risk_reason=reason,
                is_fallback=signal.is_fallback,
                llm_latency_ms=llm_metrics.latency_ms if llm_metrics else None,
                llm_prompt_tokens=llm_metrics.prompt_tokens if llm_metrics else None,
                llm_completion_tokens=llm_metrics.completion_tokens if llm_metrics else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to persist decision", symbol=signal.symbol, error=str(exc))
            return None

        if snapshot is not None:
            try:
                candles_json = json.dumps([c.model_dump(mode="json") for c in snapshot.candles])
                indicators_json = json.dumps(snapshot.indicators)
                await self._storage.save_market_snapshot(
                    symbol=snapshot.symbol,
                    timeframe=snapshot.timeframe,
                    candles_json=candles_json,
                    indicators_json=indicators_json,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "failed to persist market snapshot", symbol=signal.symbol, error=str(exc)
                )

        logger.info(
            "decision stored",
            symbol=signal.symbol,
            decision_id=decision_id,
            action=signal.action.value,
            risk_verdict=verdict,
        )
        return decision_id

    async def get_portfolio_state(self) -> PortfolioState:
        """Build current portfolio state from executor positions.

        Public: agents (daily-value updates, portfolio persistence) and tests use
        it; it used to be ``_get_portfolio_state``, an encapsulation leak pinned
        into the test contract (§7.19).
        """
        positions = await self.executor.get_positions()
        cash = await self.executor.get_cash()
        return PortfolioState(cash=cash, positions=positions)

    async def _awaiting_new_bar(
        self, symbol: str, timeframe: str, closed: list[OHLCV]
    ) -> str | None:
        """Reason to skip the LLM when the latest closed bar was already decided on.

        The last real (non-fallback) decision time comes from memory, else from
        storage once — so a restart doesn't re-ask the same bar. ``None`` (ask) when
        timing is unknown or nothing was decided yet: never skip on a guess.
        """
        if not closed:
            return None
        bar_closed_at = bar_close_time(closed[-1], timeframe)
        if bar_closed_at is None:
            return None
        last = self._last_decision_at.get(symbol)
        if last is None and self._storage is not None:
            try:
                rows = await self._storage.get_recent_decisions(symbol, limit=1)
            except Exception as exc:  # noqa: BLE001 - unknown history → just ask
                logger.warning("last-decision lookup failed", symbol=symbol, error=str(exc))
                rows = []
            if rows and rows[0].timestamp is not None:
                ts = rows[0].timestamp
                last = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
                self._last_decision_at[symbol] = last
        if last is not None and last >= bar_closed_at:
            return f"awaiting new {timeframe} bar (last decision {last:%Y-%m-%d %H:%M} UTC)"
        return None

    def _book_context(self, symbol: str, portfolio: PortfolioState) -> BookContext:
        """This symbol's slice of the book for the prompt (§7.45)."""
        position = next(
            (
                p
                for p in portfolio.positions
                if p.symbol == symbol and p.quantity > 0 and p.side == PositionSide.LONG
            ),
            None,
        )
        return BookContext(
            position=position,
            cash=portfolio.cash,
            total_value=portfolio.total_value,
            max_position_pct=self.risk_engine.settings.max_position_pct,
        )

    def _mark_positions(self, symbol: str, snapshot: MarketSnapshot) -> None:
        """Re-mark open executor positions at the snapshot's last close.

        Uses the optional ``update_price(symbol, price)`` hook implemented by
        :class:`PaperExecutor`: without it a paper position keeps its fill
        price forever, so unrealized PnL stays 0 and the daily-loss rule can
        never see market moves. The ccxt spot executor uses it to value its ledger
        positions (§7.41); XTB reports live venue prices and has no hook.
        Fail-soft: a marking failure must never break a trading cycle.
        """
        if not snapshot.candles:
            return
        last_close = snapshot.candles[-1].close
        if last_close <= 0:
            return
        update_price = getattr(self.executor, "update_price", None)
        if not callable(update_price):
            return
        try:
            update_price(symbol, last_close)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to mark position price", symbol=symbol, error=str(exc))

    def _calculate_quantity(
        self,
        signal: TradeSignal,
        portfolio: PortfolioState,
        current_price: float | None = None,
    ) -> float:
        """Calculate position size based on risk settings and available cash."""
        return calculate_quantity(
            signal,
            portfolio,
            self.risk_engine.settings,
            current_price,
            cost_factor=buy_cost_factor(self.executor),
            cost_model=CostModel.from_attrs(self.executor),
        )


# ── Shared trading rules (live pipeline ⇄ backtester, §7.14) ──────


def exit_level_breach(position: Position, mark: float) -> str | None:
    """Return ``"stop_loss"``/``"take_profit"`` when *mark* breaches the position's level.

    Single source for §7.9 semantics so the live pipeline and the backtester's replay
    can never drift apart. Side-aware (§7.48): a long stops out *below* its stop and
    takes profit above its target; a short is the mirror image (stop above entry,
    target below).
    """
    short = position.side == PositionSide.SHORT
    if position.stop_loss is not None and (
        mark >= position.stop_loss if short else mark <= position.stop_loss
    ):
        return "stop_loss"
    if position.take_profit is not None and (
        mark <= position.take_profit if short else mark >= position.take_profit
    ):
        return "take_profit"
    return None


def closing_side(position: Position) -> OrderSide:
    """The order side that *reduces* ``position``: SELL a long, BUY (cover) a short (§7.48)."""
    return OrderSide.BUY if position.side == PositionSide.SHORT else OrderSide.SELL


def buy_cost_factor(executor: object) -> float:
    """Per-unit cash cost of a BUY relative to its quoted price (§7.59 L1).

    ``(1 + slippage) × (1 + fee + fx)`` for executors that model them (paper);
    1.0 for venues that report neither. Non-numeric attributes (mocks) count as
    zero. A venue *minimum commission* (§7.65) is not linear in quantity, so it
    cannot fold into a factor — sizing uses
    :meth:`CostModel.max_affordable_fill_notional` when one is configured.
    """
    return CostModel.from_attrs(executor).buy_cost_factor


def calculate_quantity(
    signal: TradeSignal,
    portfolio: PortfolioState,
    settings: RiskSettings,
    current_price: float | None = None,
    cost_factor: float = 1.0,
    cost_model: CostModel | None = None,
) -> float:
    """Position size: ``max_position_pct`` of total value, cash- and holdings-clamped.

    Used by the live pipeline *and* the decision-replay backtester so sizing rules
    stay identical between them (§7.14). A BUY only fills the *headroom* left under
    the per-position cap — what is already held in the symbol counts (§7.42). A SELL
    closes the **whole** long position (§7.47: SELL means "close", never a slice).

    ``cost_factor`` (:func:`buy_cost_factor`) makes the cash clamp include slippage
    and fees, so a cash-bound BUY the gate approved is never rejected by the
    executor for "insufficient cash" (§7.59 L1). Pass the executor's full
    :class:`CostModel` as ``cost_model`` to additionally honour a venue *minimum
    commission* (§7.65) — it dominates ``cost_factor`` when given. Quantities
    round *down* to 1e-8 — rounding up could exceed cash (§7.59 L2); a SELL
    returns the held size exactly.
    """
    max_position_value = portfolio.total_value * settings.max_position_pct
    if signal.action == Action.BUY:
        max_position_value = max(0.0, max_position_value - long_exposure(portfolio, signal.symbol))

    price = current_price if (current_price is not None and current_price > 0) else signal.stop_loss
    if price is None or price <= 0:
        price = 1.0

    quantity = max_position_value / price

    # Optional risk-per-trade sizing (§7.54): cap the BUY so the distance to the
    # stop can never lose more than ``risk_per_trade_pct`` of total value. Only a
    # sane stop below price shrinks the trade; geometry gates reject the rest.
    if (
        signal.action == Action.BUY
        and settings.risk_per_trade_pct > 0
        and signal.stop_loss is not None
        and signal.stop_loss < price
    ):
        risk_budget = portfolio.total_value * settings.risk_per_trade_pct
        quantity = min(quantity, risk_budget / (price - signal.stop_loss))

    # Ensure we don't spend more cash than available for buys — at the executor's
    # all-in unit cost, not the quoted price (§7.59 L1), honouring a venue minimum
    # commission when the profile has one (§7.65).
    if signal.action == Action.BUY:
        if cost_model is not None:
            fill_price = price * (1.0 + cost_model.slippage_pct)
            affordable = cost_model.max_affordable_fill_notional(portfolio.cash)
        else:
            fill_price = price * max(cost_factor, 1.0)
            affordable = portfolio.cash
        max_qty_by_cash = affordable / fill_price if fill_price > 0 else 0.0
        quantity = min(quantity, max_qty_by_cash)

    # A SELL closes the held long in full (§7.47) — never more than is held
    # (§7.19), and never a cap-sized slice that leaves a remainder behind.
    if signal.action == Action.SELL:
        held = sum(
            p.quantity
            for p in portfolio.positions
            if p.symbol == signal.symbol and p.side == PositionSide.LONG
        )
        if held > 0:
            # Exactly what is held: rounding could leave dust or overshoot (§7.59 L2).
            return held

    return math.floor(max(quantity, 0.0) * 1e8) / 1e8
