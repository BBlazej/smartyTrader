"""Prompt construction for the LLM decision (moved out of ``core/decision_pipeline.py``
in §7.17).

:func:`build_user_prompt` renders a market snapshot (+ computed indicators and the
agent's own recent decisions with realized outcomes) into the structured user
prompt; :data:`DEFAULT_SYSTEM_PROMPT` is the conservative-analyst persona used when
no override is configured. Pure string building — no I/O.
"""

from __future__ import annotations

from ..core.models import DecisionRecord, MarketSnapshot


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


DEFAULT_SYSTEM_PROMPT = """\
You are a conservative quantitative trading analyst. You turn market data and \
technical indicators into structured trade signals. Calibrate your confidence to \
the strength and agreement of the evidence — do not inflate it. Prefer HOLD when the \
data is mixed, ranging, or thin. Only act (BUY/SELL) on a clear, consistent edge, and \
then always provide sensible stop_loss and take_profit levels. Cite the specific \
indicators that drove your call in the reasoning.\n\
Respond with ONLY a JSON object (no prose, no code fences) of the form:\n\
{"symbol": str, "action": "buy" | "sell" | "hold", "confidence": float in [0, 1], \
"reasoning": str, "stop_loss": float | null, "take_profit": float | null}"""
