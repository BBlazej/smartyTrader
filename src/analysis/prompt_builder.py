"""Prompt construction for the LLM decision (moved out of ``core/decision_pipeline.py``
in §7.17).

:func:`build_user_prompt` renders a market snapshot (+ computed indicators and the
agent's own recent decisions with realized outcomes) into the structured user
prompt; :data:`DEFAULT_SYSTEM_PROMPT` is the conservative-analyst persona used when
no override is configured. Pure string building — no I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..core.models import (
    BookContext,
    DecisionRecord,
    EventKind,
    MarketSnapshot,
    PositionSide,
    SymbolContext,
)
from .candles import split_forming
from .sanitize import safe_label


def build_user_prompt(
    snapshot: MarketSnapshot,
    prior_decisions: list[DecisionRecord] | None = None,
    book: BookContext | None = None,
    context: SymbolContext | None = None,
) -> str:
    """Build a structured prompt from the market snapshot and indicators.

    When ``prior_decisions`` is provided (most recent first), it is rendered under a
    ``CONTEXT`` section so the LLM can learn from the agent's own track record.
    ``book`` adds a ``YOUR BOOK`` section — the position held in this symbol, cash
    and the position limit (§7.45) — so the model knows whether a BUY opens or adds
    and whether a SELL has anything to close. ``context`` adds a ``MARKET CONTEXT``
    section (§7.18) — sentiment, scheduled events, venue notices and the news digest
    card, structured fields only (raw news text never reaches this prompt).
    """
    lines: list[str] = []

    lines.append(f"Symbol: {snapshot.symbol} ({snapshot.timeframe})")
    # The venue's last bar may still be forming (§7.56): it is the live price, but
    # its OHLCV is provisional — labelled below and excluded from the indicators.
    _, forming = split_forming(snapshot.candles, snapshot.timeframe, snapshot.fetched_at)

    # Current price (last close) — the reference for stop_loss / take_profit.
    if snapshot.candles:
        live = " (live — current bar still forming)" if forming is not None else ""
        lines.append(f"Current price: {_price(snapshot.candles[-1].close)}{live}")

    # Recent candles (last 5)
    recent = snapshot.candles[-5:] if len(snapshot.candles) >= 5 else snapshot.candles
    lines.append("")
    lines.append("Recent candles (OHLCV):")
    for c in recent:
        ts = c.timestamp.isoformat() if c.timestamp else "N/A"
        tag = "  [FORMING — bar not closed, values provisional]" if c is forming else ""
        lines.append(
            f"  {ts}: O={_price(c.open)} H={_price(c.high)} L={_price(c.low)} "
            f"C={_price(c.close)} V={c.volume:.0f}{tag}"
        )

    # Indicators
    if snapshot.indicators:
        lines.append("")
        lines.append(
            "Technical indicators (closed bars only):"
            if forming is not None
            else "Technical indicators:"
        )
        for key, value in snapshot.indicators.items():
            lines.append(f"  {key}: {value}")

    if book is not None:
        lines.append("")
        lines.extend(_render_book(snapshot.symbol, book))

    if context is not None:
        lines.append("")
        lines.extend(_render_context(context))

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
    if book.strategy is not None:
        stop = (
            f" — time stop: a position auto-closes after {_hours(book.max_holding_hours)}"
            if book.max_holding_hours is not None
            else ""
        )
        held = (
            f" (this one held {_hours(book.held_hours)})"
            if book.held_hours is not None and held_value > 0
            else ""
        )
        lines.append(f"  Strategy sleeve: {book.strategy}{stop}{held}")
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


#: Display names for sentiment sources (§7.18).
_SENTIMENT_NAMES: dict[str, str] = {"fear_greed": "Crypto Fear & Greed index (0-100)"}
#: At most this many scheduled events are listed.
_MAX_EVENTS = 10
#: Events this recent still show (their after-window may still be open).
_RECENT_EVENTS = timedelta(hours=6)


def _when(at: datetime, now: datetime) -> str:
    delta = (at - now).total_seconds() / 3600.0
    return f"in {_hours(delta)}" if delta >= 0 else f"{_hours(-delta)} ago"


def _render_context(ctx: SymbolContext) -> list[str]:
    """The ``MARKET CONTEXT`` section (§7.18): structured external data, labelled as such.

    Every external string is reduced by :func:`safe_label`; the section states that
    context informs but never overrides the price evidence (CHANGE.md §4.5), and that
    entry blackouts are enforced by the risk gate regardless of the answer.
    """
    now = ctx.now
    lines = [
        (
            "MARKET CONTEXT (external data — it informs your judgement but never overrides "
            "the price evidence above; event blackouts are enforced by the risk gate):"
        )
    ]
    if ctx.entry_blackout:
        lines.append(
            f"  ENTRY BLACKOUT: {safe_label(ctx.entry_blackout, 200)}. A BUY now will be "
            "rejected by the risk gate; HOLD or closing a held position remain possible."
        )
    if ctx.sentiment is not None:
        reading = ctx.sentiment
        name = _SENTIMENT_NAMES.get(reading.source, safe_label(reading.source, 30))
        label = f" ({safe_label(reading.label, 40)})" if reading.label else ""
        lines.append(
            f"  Sentiment: {name} {reading.value:.0f}{label}, "
            f"as of {reading.as_of:%Y-%m-%d %H:%M} UTC"
        )
    horizon = now + timedelta(hours=ctx.lookahead_hours)
    shown = [e for e in ctx.events if now - _RECENT_EVENTS <= e.at <= horizon][:_MAX_EVENTS]
    if shown:
        lines.append(
            f"  Scheduled events (last {_hours(6)} and next {_hours(ctx.lookahead_hours)}):"
        )
        for event in shown:
            if event.kind is EventKind.EARNINGS:
                what = f"{safe_label(event.asset or '', 20)} earnings release"
            else:
                what = f"{safe_label(event.currency or '', 10)} {safe_label(event.title, 80)}"
            lines.append(
                f"    {event.at:%Y-%m-%d %H:%M} UTC ({_when(event.at, now)}) — {what} "
                f"[{event.importance.value} impact]"
            )
    else:
        lines.append(
            f"  Scheduled events: none on record in the next {_hours(ctx.lookahead_hours)}."
        )
    for notice in ctx.notices[-3:]:
        lines.append(
            f"  Venue notice: the exchange announced a DELISTING of "
            f"{safe_label(notice.asset or '', 20)} on {notice.at:%Y-%m-%d} — "
            "new entries are blocked; plan an orderly exit of any position."
        )
    card = ctx.card
    if card is not None:
        lines.append(
            f"  News digest (as of {card.as_of:%Y-%m-%d %H:%M} UTC, from {len(card.sources)} "
            f"source(s), digest confidence {card.confidence:.2f}): news sentiment "
            f"{card.sentiment:+.2f} on a -1..+1 scale"
        )
        for catalyst in card.catalysts:
            label = safe_label(catalyst, 160)
            if label:
                lines.append(f"    - {label}")
        if card.event_risk:
            upcoming = ", ".join(f"{e.type} {e.date}" for e in card.event_risk)
            lines.append(f"    Mentioned upcoming: {upcoming}")
    return lines


def _hours(value: float | None) -> str:
    """Holding time as hours, or days once it is at least two days."""
    if value is None:
        return "n/a"
    return f"{value / 24:.1f}d" if value >= 48 else f"{value:.1f}h"


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


#: Per-sleeve playbooks (§7.71, CHANGE.md §4.5) appended to the system prompt so the
#: model stops mixing styles. Keys must equal ``config.SLEEVE_PLAYBOOKS``.
PLAYBOOKS: dict[str, str] = {
    "swing": (
        "STRATEGY PLAYBOOK — SWING (holds of hours to about three days). Trade short-term "
        "momentum and mean-reversion setups on these bars. Keep stops tight (roughly 1-2 ATR "
        "below entry) and take-profits near. A position still open when this sleeve's time "
        "stop elapses is closed automatically, so only enter setups expected to play out "
        "within that window. Prefer HOLD when the move is already extended."
    ),
    "position": (
        "STRATEGY PLAYBOOK — POSITION (holds of days to weeks). Follow the established trend "
        "on these higher-timeframe bars and ignore intraday noise. Enter only when trend and "
        "momentum agree; place wider stops (beyond the recent swing low, roughly 2-4 ATR) and "
        "distant take-profits. Expect few trades — most decisions should be HOLD. SELL when "
        "the trend clearly breaks, not on a single red bar."
    ),
}


def system_prompt_for(playbook: str | None) -> str:
    """The system prompt for a sleeve: the default persona plus its playbook."""
    if playbook is None:
        return DEFAULT_SYSTEM_PROMPT
    return f"{DEFAULT_SYSTEM_PROMPT}\n\n{PLAYBOOKS[playbook]}"
