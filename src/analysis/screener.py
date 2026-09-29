"""Deterministic market screener (PLAN §7.70, CHANGE.md §4.4).

Universe selection without an LLM: over the symbols the executor can actually
trade, apply a **liquidity floor** (24 h quote volume), a **volatility band**
(daily-return std dev — dead-flat and blow-up instruments are both useless to a
swing book), then rank by **momentum** (trailing return) with an
unusual-volume flag carried along for audit. Everything here is pure,
deterministic math over already-fetched data — the same inputs always produce
the same ranking, per the project rule that universe limits are code + config,
never model judgement (CHANGE.md §3).

The watchlist manager (:class:`src.core.watchlist.WatchlistManager`) feeds this
module; nothing here touches storage or the network.
"""

from __future__ import annotations

import itertools
import statistics

from pydantic import BaseModel

from ..core.models import OHLCV


class ScreenMetrics(BaseModel):
    """Per-symbol metrics computed from *daily* candles (closed bars)."""

    momentum: float  # trailing return over the momentum window, e.g. 0.12 = +12 %
    daily_volatility: float  # std dev of daily close-to-close returns (per day)
    # Latest day's volume divided by the mean of the prior days (>1 = unusual).
    volume_spike: float | None = None


class ScreenedSymbol(BaseModel):
    """One candidate that survived every filter, with its ranking inputs."""

    symbol: str
    quote_volume_24h: float
    metrics: ScreenMetrics
    rank: int = 0  # 1 = best momentum, assigned by :func:`rank_candidates`
    news_mentions: int = 0  # recent news items naming it (§7.83), when counted


def filter_by_liquidity(
    quote_volumes: dict[str, float],
    min_quote_volume_24h: float,
) -> list[tuple[str, float]]:
    """Keep symbols whose 24 h quote volume clears the floor.

    Returns ``(symbol, volume)`` pairs sorted by volume descending (ties broken
    by symbol so the order is deterministic). Non-positive/missing volumes are
    dropped — an instrument we cannot measure liquidity on is not tradeable.
    """
    kept = [
        (symbol, float(volume))
        for symbol, volume in quote_volumes.items()
        if volume is not None and float(volume) > 0 and float(volume) >= min_quote_volume_24h
    ]
    return sorted(kept, key=lambda pair: (-pair[1], pair[0]))


def compute_screen_metrics(
    candles: list[OHLCV],
    momentum_days: int,
    lookback_days: int,
) -> ScreenMetrics | None:
    """Compute momentum / volatility / volume-spike from daily candles.

    ``momentum`` is the return over the last ``momentum_days`` closes;
    ``daily_volatility`` is the std dev of close-to-close returns over at most
    ``lookback_days``. Returns ``None`` when the series is too short to compute
    either honestly — we never fabricate metrics from sparse data (the same rule
    as the candle-depth guards in §7.11).
    """
    if momentum_days < 1 or lookback_days < 2:
        raise ValueError("momentum_days must be >= 1 and lookback_days >= 2")
    closes = [candle.close for candle in candles if candle.close is not None]
    window = closes[-(lookback_days + 1) :]
    # Need the momentum reference close plus at least two returns for a std dev.
    if len(window) < momentum_days + 2:
        return None
    reference = window[-(momentum_days + 1)]
    if reference <= 0:
        return None
    momentum = window[-1] / reference - 1.0
    returns = [
        later / earlier - 1.0 for earlier, later in itertools.pairwise(window) if earlier > 0
    ]
    if len(returns) < 2:
        return None
    daily_volatility = statistics.pstdev(returns)

    volumes = [candle.volume for candle in candles if candle.volume is not None]
    volume_spike: float | None = None
    if len(volumes) >= 3:
        prior = volumes[-(lookback_days + 1) : -1] or volumes[:-1]
        mean_prior = sum(prior) / len(prior)
        if mean_prior > 0:
            volume_spike = volumes[-1] / mean_prior
    return ScreenMetrics(
        momentum=momentum,
        daily_volatility=daily_volatility,
        volume_spike=volume_spike,
    )


def prioritize_mentioned(
    ranked: list[ScreenedSymbol], mentions: dict[str, int], min_mentions: int
) -> list[ScreenedSymbol]:
    """Move candidates with ``>= min_mentions`` recent news items ahead (§7.83).

    CHANGE.md §4.4: "screener rank + news mentions". Mentions only *reorder* the
    candidates that already passed every screener filter — a mentioned symbol never
    bypasses the liquidity floor or volatility band — and screener order is kept
    within each group (stable sort), so the result stays fully deterministic.
    """
    for screened in ranked:
        screened.news_mentions = int(mentions.get(screened.symbol, 0))
    return sorted(ranked, key=lambda c: c.news_mentions < min_mentions)


def rank_candidates(
    candidates: list[tuple[str, float, ScreenMetrics]],
    min_daily_volatility: float = 0.0,
    max_daily_volatility: float | None = None,
) -> list[ScreenedSymbol]:
    """Apply the volatility band and rank by momentum (best first).

    ``candidates`` are ``(symbol, quote_volume_24h, metrics)`` triples. Ties in
    momentum break by volume descending, then symbol — fully deterministic.
    """
    passed: list[ScreenedSymbol] = []
    for symbol, volume, metrics in candidates:
        if metrics.daily_volatility < min_daily_volatility:
            continue
        if max_daily_volatility is not None and metrics.daily_volatility > max_daily_volatility:
            continue
        passed.append(ScreenedSymbol(symbol=symbol, quote_volume_24h=volume, metrics=metrics))
    passed.sort(key=lambda c: (-c.metrics.momentum, -c.quote_volume_24h, c.symbol))
    for position, screened in enumerate(passed, start=1):
        screened.rank = position
    return passed
