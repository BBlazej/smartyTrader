"""Decision pipeline — orchestrates fetch → indicators → LLM → risk → execute."""

from __future__ import annotations

from typing import Any, Protocol

import structlog

from .llm_client import LLMClient
from .models import (
    Action,
    DecisionRecord,
    Executor,
    MarketSnapshot,
    OrderResult,
    OrderSide,
    PortfolioState,
    RiskResult,
    RiskVerdict,
    TradeSignal,
)
from .risk_engine import RiskEngine
from .storage import Storage

logger = structlog.get_logger()


# ── Protocols for swappable components ────────────────────────


class MarketDataProvider(Protocol):
    """Provides market data for a symbol."""

    async def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot: ...


# ── Pipeline Result ───────────────────────────────────────────


class PipelineStep(str):
    FETCH_DATA = "fetch_data"
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
    ) -> None:
        self.symbol = symbol
        self.signal = signal
        self.risk_result = risk_result
        self.order_result = order_result
        self.snapshot = snapshot
        self.error = error

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
    ) -> None:
        self.provider = provider
        self.llm_client = llm_client
        self.risk_engine = risk_engine
        self.executor = executor
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        self._storage = storage
        self._decision_history_limit = decision_history_limit

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

        return [
            DecisionRecord(
                action=row.action,
                confidence=row.confidence,
                reasoning=row.reasoning,
                risk_verdict=row.risk_verdict,
                risk_reason=row.risk_reason,
                realized_pnl=row.realized_pnl,
                timestamp=row.timestamp,
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

        # Step 1b — Mark open positions to this cycle's close (paper mode).
        # Done before the risk check so the portfolio handed to the risk engine
        # — and the daily-loss baseline the agent updates afterwards — reflects
        # market moves, not a position frozen at its fill price.
        self._mark_positions(symbol, snapshot)

        # Step 2 — Compute indicators
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.COMPUTE_INDICATORS)
        try:
            snapshot.indicators = compute_indicators(snapshot.candles)
        except Exception as exc:  # noqa: BLE001
            step_logger.error("indicator computation failed", error=str(exc))
            return PipelineResult(
                symbol=symbol, snapshot=snapshot, error=f"Indicator computation failed: {exc}"
            )

        # Step 3 — Build prompt (with the agent's own recent decisions for context)
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.BUILD_PROMPT)
        prior_decisions = await self.get_recent_decisions(symbol)
        user_prompt = build_user_prompt(snapshot, prior_decisions)

        # Step 4 — Call LLM
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.CALL_LLM)
        signal = await self.llm_client.ask_trade_signal(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
        )
        signal.symbol = symbol  # Ensure symbol is set

        # Step 5 — Risk check
        step_logger = logger.bind(symbol=symbol, step=PipelineStep.RISK_CHECK)
        portfolio = await self._get_portfolio_state()
        risk_result = self.risk_engine.evaluate(signal, portfolio)

        if risk_result.verdict == RiskVerdict.REJECTED:
            step_logger.warning(
                "risk rejected",
                reason=risk_result.reason,
                action=signal.action.value,
            )
            return PipelineResult(
                symbol=symbol, signal=signal, risk_result=risk_result, snapshot=snapshot
            )

        # Step 6 — Execute (only for BUY/SELL)
        if signal.action == Action.HOLD:
            step_logger.info("holding", confidence=signal.confidence)
            return PipelineResult(
                symbol=symbol, signal=signal, risk_result=risk_result, snapshot=snapshot
            )

        step_logger = logger.bind(symbol=symbol, step=PipelineStep.EXECUTE)
        try:
            order_side = OrderSide.BUY if signal.action == Action.BUY else OrderSide.SELL
            current_price = snapshot.candles[-1].close if snapshot.candles else None
            quantity = self._calculate_quantity(signal, portfolio, current_price=current_price)
            order_result = await self.executor.place_order(
                symbol=symbol,
                side=order_side,
                quantity=quantity,
                price=current_price,
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
            )
        except Exception as exc:  # noqa: BLE001
            step_logger.error("execution failed", error=str(exc))
            return PipelineResult(
                symbol=symbol,
                signal=signal,
                risk_result=risk_result,
                snapshot=snapshot,
                error=f"Execution failed: {exc}",
            )

    async def _get_portfolio_state(self) -> PortfolioState:
        """Build current portfolio state from executor positions."""
        positions = await self.executor.get_positions()
        cash = await self.executor.get_cash()
        return PortfolioState(cash=cash, positions=positions)

    def _mark_positions(self, symbol: str, snapshot: MarketSnapshot) -> None:
        """Re-mark open executor positions at the snapshot's last close.

        Uses the optional ``update_price(symbol, price)`` hook implemented by
        :class:`PaperExecutor`: without it a paper position keeps its fill
        price forever, so unrealized PnL stays 0 and the daily-loss rule can
        never see market moves. Executors that report live venue prices
        (Kraken, XTB) do not implement the hook and are left untouched.
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
        # Use max_position_pct from risk settings to cap position size
        max_position_value = portfolio.total_value * self.risk_engine.settings.max_position_pct

        price = (
            current_price if (current_price is not None and current_price > 0) else signal.stop_loss
        )
        if price is None or price <= 0:
            price = 1.0

        quantity = max_position_value / price

        # Ensure we don't spend more cash than available for buys
        if signal.action == Action.BUY:
            max_qty_by_cash = portfolio.cash / price
            quantity = min(quantity, max_qty_by_cash)

        return round(quantity, 8)


# ── Indicator Computation ─────────────────────────────────────


def compute_indicators(candles: list[Any]) -> dict[str, Any]:
    """Compute basic technical indicators from OHLCV candles.

    Returns a dict with RSI, MACD signal, and Bollinger Band position.
    Pure computation — no I/O.
    """
    if not candles:
        return {}

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    result: dict[str, Any] = {}

    # RSI (14-period)
    rsi = _compute_rsi(closes, period=14)
    if rsi is not None:
        result["rsi_14"] = round(rsi, 2)

    # MACD (12/26/9)
    macd_line, signal_line, histogram = _compute_macd(closes)
    if macd_line is not None:
        result["macd_line"] = round(macd_line, 4)
        result["macd_signal"] = round(signal_line, 4)
        result["macd_histogram"] = round(histogram, 4)

    # Bollinger Bands (20-period, 2σ)
    bb_upper, bb_middle, bb_lower = _compute_bollinger_bands(closes)
    if bb_upper is not None:
        result["bb_upper"] = round(bb_upper, 2)
        result["bb_middle"] = round(bb_middle, 2)
        result["bb_lower"] = round(bb_lower, 2)

        # Bandwidth and position within bands
        bandwidth = (bb_upper - bb_lower) / bb_middle if bb_middle > 0 else 0.0
        result["bb_bandwidth"] = round(bandwidth, 4)

    # ATR (14-period)
    atr = _compute_atr(highs, lows, closes, period=14)
    if atr is not None:
        result["atr_14"] = round(atr, 2)

    # Volume SMA (20-period)
    volumes = [c.volume for c in candles]
    vol_sma = _sma(volumes, 20)
    if vol_sma is not None:
        result["volume_sma_20"] = round(vol_sma, 2)

    return result


# ── Prompt Building ───────────────────────────────────────────


def build_user_prompt(
    snapshot: MarketSnapshot, prior_decisions: list[DecisionRecord] | None = None
) -> str:
    """Build a structured prompt from the market snapshot and indicators.

    When ``prior_decisions`` is provided (most recent first), it is rendered under a
    ``CONTEXT`` section so the LLM can learn from the agent's own track record.
    """
    lines: list[str] = []

    lines.append(f"Symbol: {snapshot.symbol} ({snapshot.timeframe})")

    # Current price (last close) — the reference for stop_loss / take_profit.
    if snapshot.candles:
        lines.append(f"Current price: {snapshot.candles[-1].close:.2f}")

    # Recent candles (last 5)
    recent = snapshot.candles[-5:] if len(snapshot.candles) >= 5 else snapshot.candles
    lines.append("")
    lines.append("Recent candles (OHLCV):")
    for c in recent:
        ts = c.timestamp.isoformat() if c.timestamp else "N/A"
        lines.append(
            f"  {ts}: O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f} V={c.volume:.0f}"
        )

    # Indicators
    if snapshot.indicators:
        lines.append("")
        lines.append("Technical indicators:")
        for key, value in snapshot.indicators.items():
            lines.append(f"  {key}: {value}")

    if prior_decisions:
        lines.append("")
        lines.append("CONTEXT — your most recent decisions for this symbol (most recent first):")
        for d in prior_decisions:
            parts = [
                f"action: {d.action}",
                f"confidence: {d.confidence:.2f}",
                f"risk verdict: {d.risk_verdict}",
            ]
            if d.risk_reason:
                parts.append(f"risk reason: {d.risk_reason}")
            if d.reasoning:
                parts.append(f"reasoning: {d.reasoning}")
            parts.append(_format_outcome(d.realized_pnl))
            lines.append("  " + " | ".join(parts))
        lines.append(
            "Each outcome is the net PnL once that position closed (or 'still open' if it "
            "hasn't yet). Learn from it: avoid repeating the same losing pattern and lean "
            "into setups that have paid off."
        )

    lines.append("")
    lines.append(
        "Weigh the trend, momentum, and volatility shown above. Prefer HOLD unless the "
        "indicators give a clear, consistent edge. When you act (buy/sell), set stop_loss and "
        "take_profit that are consistent with the current price and recent volatility. "
        "Return ONLY valid JSON matching the TradeSignal schema."
    )

    return "\n".join(lines)


def _format_outcome(realized_pnl: float | None) -> str:
    """Render a decision's realized outcome for the prompt.

    ``None`` means the position is still open (no closed trade yet), so we say
    so explicitly rather than implying a ``0.0`` PnL.
    """
    if realized_pnl is None:
        return "outcome: still open"
    if realized_pnl > 0:
        return f"outcome: +{realized_pnl:.2f} (win)"
    if realized_pnl < 0:
        return f"outcome: {realized_pnl:.2f} (loss)"
    return "outcome: 0.00 (flat)"


# ── Indicator Helpers ─────────────────────────────────────────


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]

    gains = [x for x in recent if x > 0]
    losses = [-x for x in recent if x < 0]

    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_macd(
    closes: list[float],
) -> tuple[float | None, float | None, float | None]:
    """Compute MACD line, signal line, and histogram."""
    if len(closes) < 26:
        return None, None, None

    ema_12 = _ema(closes, 12)
    ema_26 = _ema(closes, 26)

    if ema_12 is None or ema_26 is None:
        return None, None, None

    macd_line = ema_12 - ema_26

    # Signal line — EMA of MACD values (simplified: use last N MACD values)
    macd_values: list[float] = []
    for i in range(26, len(closes) + 1):
        chunk = closes[:i]
        e12 = _ema(chunk, 12)
        e26 = _ema(chunk, 26)
        if e12 is not None and e26 is not None:
            macd_values.append(e12 - e26)

    signal_line = _ema(macd_values, 9) if len(macd_values) >= 9 else macd_line
    histogram = macd_line - (signal_line or 0)

    return macd_line, signal_line, histogram


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # Start with SMA

    for value in values[period:]:
        ema = (value - ema) * multiplier + ema

    return ema


def _compute_bollinger_bands(
    closes: list[float],
) -> tuple[float | None, float | None, float | None]:
    if len(closes) < 20:
        return None, None, None

    window = closes[-20:]
    middle = sum(window) / 20
    variance = sum((x - middle) ** 2 for x in window) / 20
    std_dev = variance**0.5

    upper = middle + 2 * std_dev
    lower = middle - 2 * std_dev

    return upper, middle, lower


def _compute_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    if len(closes) < period + 1:
        return None

    true_ranges: list[float] = []
    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    recent = true_ranges[-period:]
    return sum(recent) / period


# ── Default System Prompt ─────────────────────────────────────

_DEFAULT_SYSTEM_PROMPT = """\
You are a conservative quantitative trading analyst. You turn market data and \
technical indicators into structured trade signals. Calibrate your confidence to \
the strength and agreement of the evidence — do not inflate it. Prefer HOLD when the \
data is mixed, ranging, or thin. Only act (BUY/SELL) on a clear, consistent edge, and \
then always provide sensible stop_loss and take_profit levels. Cite the specific \
indicators that drove your call in the reasoning.\n\
Respond with ONLY a JSON object (no prose, no code fences) of the form:\n\
{\"symbol\": str, \"action\": "buy" | "sell" | "hold", \"confidence\": float in [0, 1], \
\"reasoning\": str, \"stop_loss\": float | null, \"take_profit\": float | null}"""
