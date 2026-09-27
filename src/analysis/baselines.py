"""Dumb baselines every strategy has to beat (§7.73, CHANGE.md §4.2).

A strategy sleeve only earns more than its floor weight if it beats the best of
these on the same capital, period and cost model: **buy & hold**, a **20/50
moving-average crossover** on the same timeframe, and **cash** (0 %). An LLM with no
proven edge losing to "just hold it" after fees is the most likely honest outcome
(CHANGE.md §2) — these make that answer cheap to see.

Pure math over closed candles, deterministic, no look-ahead: the crossover decides
on bar *i*'s close and earns bar *i → i+1*'s return. Every position change pays
``cost_pct`` (fee + slippage per side), including the final exit back to cash so a
baseline is compared as realized, like a sleeve's closed trades.
"""

from __future__ import annotations

from ..core.models import OHLCV


def _closes(candles: list[OHLCV]) -> list[float]:
    return [c.close for c in candles if c.close is not None and c.close > 0]


def buy_and_hold_return(candles: list[OHLCV], cost_pct: float = 0.0) -> float | None:
    """Return of buying the first close and selling the last, net of both sides' cost."""
    closes = _closes(candles)
    if len(closes) < 2:
        return None
    return closes[-1] / closes[0] * (1.0 - cost_pct) ** 2 - 1.0


def ma_crossover_return(
    candles: list[OHLCV], fast: int = 20, slow: int = 50, cost_pct: float = 0.0
) -> float | None:
    """Long while SMA(fast) > SMA(slow), else cash; ``None`` below ``slow + 1`` closes."""
    if not 0 < fast < slow:
        raise ValueError("need 0 < fast < slow")
    closes = _closes(candles)
    if len(closes) < slow + 1:
        return None
    equity = 1.0
    invested = False
    fast_sum = sum(closes[slow - fast : slow])
    slow_sum = sum(closes[:slow])
    for i in range(slow - 1, len(closes) - 1):
        if i >= slow:  # slide both windows to end at bar i
            fast_sum += closes[i] - closes[i - fast]
            slow_sum += closes[i] - closes[i - slow]
        want = fast_sum / fast > slow_sum / slow
        if want != invested:
            equity *= 1.0 - cost_pct
            invested = want
        if invested:
            equity *= closes[i + 1] / closes[i]
    if invested:
        equity *= 1.0 - cost_pct
    return equity - 1.0


def equal_weight(returns: list[float | None]) -> float | None:
    """Equal-weighted blend across symbols (symbols without enough data are skipped)."""
    usable = [r for r in returns if r is not None]
    return sum(usable) / len(usable) if usable else None


def baseline_returns(
    candles_by_symbol: dict[str, list[OHLCV]], cost_pct: float = 0.0
) -> dict[str, float | None]:
    """``{baseline: equal-weighted return}`` over the symbols — ``cash`` is always 0."""
    series = list(candles_by_symbol.values())
    return {
        "buy_and_hold": equal_weight([buy_and_hold_return(c, cost_pct) for c in series]),
        "ma_crossover_20_50": equal_weight(
            [ma_crossover_return(c, cost_pct=cost_pct) for c in series]
        ),
        "cash": 0.0,
    }


def best_baseline(baselines: dict[str, float | None]) -> tuple[str, float] | None:
    """The strongest baseline (ties → first listed)."""
    usable = [(name, value) for name, value in baselines.items() if value is not None]
    return max(usable, key=lambda item: item[1]) if usable else None
