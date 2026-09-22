"""Technical indicator computation (moved out of ``core/decision_pipeline.py`` in §7.17).

Pure functions over OHLCV candles — no I/O, no network, deterministic. The decision
pipeline calls :func:`compute_indicators` each cycle and feeds the result into the
snapshot's ``indicators`` map (which the prompt renders verbatim to the LLM).

Methodology note: RSI/ATR here use **simple averages** over the last ``period``
deltas/true-ranges rather than Wilder smoothing, so values can differ from
TradingView/pandas-ta readings — documented so nobody misreads the LLM-facing
numbers (also tracked in PLAN §7.19).
"""

from __future__ import annotations

from typing import Any


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


# ── Indicator Helpers ─────────────────────────────────────────


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """RSI over the last ``period`` deltas using a **simple average** (not Wilder
    smoothing) — expect different readings from TradingView/pandas-ta (§7.19)."""
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
    """Compute MACD line, signal line, and histogram.

    O(N): the EMA *series* is walked once per period (§7.37, find #11's math
    half — the old prefix-reslice loop was O(N²) on long stock histories).
    Values are bit-identical to recomputing :func:`_ema` per prefix, since each
    entry of the series *is* the EMA of that prefix.
    """
    if len(closes) < 26:
        return None, None, None

    ema_12 = _ema(closes, 12)
    ema_26 = _ema(closes, 26)

    if ema_12 is None or ema_26 is None:
        return None, None, None

    macd_line = ema_12 - ema_26

    # Signal line — EMA of the MACD series (from the first index both EMAs exist,
    # i.e. prefixes of length 26..N, exactly as before).
    e12_series = _ema_series(closes, 12)
    e26_series = _ema_series(closes, 26)
    macd_values = [
        e12 - e26
        for e12, e26 in zip(e12_series[25:], e26_series[25:])
        if e12 is not None and e26 is not None
    ]

    signal_line = _ema(macd_values, 9) if len(macd_values) >= 9 else macd_line
    histogram = macd_line - (signal_line or 0)

    return macd_line, signal_line, histogram


def _ema_series(values: list[float], period: int) -> list[float | None]:
    """Running EMA of every prefix: ``out[k]`` is the EMA of ``values[:k+1]``
    (``None`` while fewer than ``period`` seeds exist). One pass, O(N) (§7.37)."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out

    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # Start with SMA
    out[period - 1] = ema
    for k in range(period, len(values)):
        ema = (values[k] - ema) * multiplier + ema
        out[k] = ema
    return out


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
    """ATR as the **simple average** true range over ``period`` candles (not Wilder
    smoothed) — expect different readings from TradingView/pandas-ta (§7.19)."""
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
