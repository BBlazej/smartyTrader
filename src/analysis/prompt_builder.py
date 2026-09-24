"""Prompt construction for the LLM decision (moved out of ``core/decision_pipeline.py``
in §7.17).

:func:`build_user_prompt` renders a market snapshot (+ computed indicators and the
agent's own recent decisions with realized outcomes) into the structured user
prompt; :data:`DEFAULT_SYSTEM_PROMPT` is the conservative-analyst persona used when
no override is configured. Pure string building — no I/O.
"""

from __future__ import annotations

from ..core.models import BookContext, DecisionRecord, MarketSnapshot, PositionSide


def build_user_prompt(
    snapshot: MarketSnapshot,
    prior_decisions: list[DecisionRecord] | None = None,
    book: BookContext | None = None,
) -> str:
    """Build a structured prompt from the market snapshot and indicators.

    When ``prior_decisions`` is provided (most recent first), it is rendered under a
    ``CONTEXT`` section so the LLM can learn from the agent's own track record.
    ``book`` adds a ``YOUR BOOK`` section — the position held in this symbol, cash
    and the position limit (§7.45) — so the model knows whether a BUY opens or adds
    and whether a SELL has anything to close.
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

    if book is not None:
        lines.append("")
        lines.extend(_render_book(snapshot.symbol, book))

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
            parts.append(_format_outcome(d))
            lines.append("  " + " | ".join(parts))
        lines.append(
            "Each outcome is the net PnL once that position closed ('still open' while it "
            "hasn't; 'n/a' when the decision never traded). Learn from it: avoid repeating "
            "the same losing pattern and lean into setups that have paid off."
        )

    lines.append("")
    lines.append(
        "Weigh the trend, momentum, and volatility shown above. Prefer HOLD unless the "
        "indicators give a clear, consistent edge. When you act (buy/sell), set stop_loss and "
        "take_profit that are consistent with the current price and recent volatility. "
        "Return ONLY valid JSON matching the TradeSignal schema."
    )

    return "\n".join(lines)


def _render_book(symbol: str, book: BookContext) -> list[str]:
    """The ``YOUR BOOK`` section: this symbol's position, cash and the size limit (§7.45)."""
    lines = ["YOUR BOOK (spot account — you can only hold longs; SELL closes, it never shorts):"]
    pos = book.position
    equity = book.total_value
    if pos is None or pos.quantity <= 0 or pos.side != PositionSide.LONG:
        lines.append(f"  Position in {symbol}: none (flat) — a SELL has nothing to close.")
        held_value = 0.0
    else:
        held_value = pos.quantity * pos.current_price
        share = f", {held_value / equity:.1%} of equity" if equity > 0 else ""
        levels = []
        if pos.stop_loss is not None:
            levels.append(f"stop {_price(pos.stop_loss)}")
        if pos.take_profit is not None:
            levels.append(f"take-profit {_price(pos.take_profit)}")
        lines.append(
            f"  Position in {symbol}: LONG {pos.quantity:.8g} @ avg {_price(pos.avg_entry_price)}, "
            f"mark {_price(pos.current_price)}, unrealized {pos.pnl:+.2f} ({pos.pnl_pct:+.2%})"
            f"{share}" + (f"; active {', '.join(levels)}" if levels else "")
        )
    lines.append(f"  Cash: {book.cash:.2f} of total equity {equity:.2f}")
    if book.max_position_pct is not None and equity > 0:
        cap = book.max_position_pct * equity
        headroom = max(0.0, cap - held_value)
        lines.append(
            f"  Position limit: {book.max_position_pct:.0%} of equity per symbol "
            f"({cap:.2f}); room to add: {headroom:.2f}"
            + (" — at the limit, a BUY will be rejected." if headroom <= 0 else "")
        )
    return lines


def _price(value: float) -> str:
    """Price with enough precision for sub-dollar assets too."""
    return f"{value:.2f}" if abs(value) >= 1 else f"{value:.6g}"


def _format_outcome(d: DecisionRecord) -> str:
    """Render a decision's realized outcome for the prompt.

    A number once a trade closed; ``still open`` only for an executed entry whose
    position has not closed yet; ``n/a`` for decisions that never traded — HOLDs,
    risk rejections and unfilled orders used to render as "still open" forever,
    telling the model it held trades that never existed (§7.45).
    """
    realized_pnl = d.realized_pnl
    if realized_pnl is None:
        if d.action == "hold":
            return "outcome: n/a (hold — no trade)"
        if d.risk_verdict != "approved":
            return "outcome: n/a (rejected — not executed)"
        if d.filled is False:
            return "outcome: n/a (order not filled)"
        if d.action == "sell":
            return "outcome: n/a (no tracked position closed)"
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
