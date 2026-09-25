"""Indicator math tests (§7.37): O(N) MACD must be bit-identical to the old
prefix-recompute implementation, plus calendar-aware Sharpe annualization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.analysis.indicators import _compute_macd, _ema, _ema_series
from src.core.backtester import estimate_periods_per_year


def _reference_macd(closes: list[float]) -> tuple[float | None, float | None, float | None]:
    """The pre-§7.37 O(N²) implementation, verbatim — the behavioral oracle."""
    if len(closes) < 26:
        return None, None, None

    ema_12 = _ema(closes, 12)
    ema_26 = _ema(closes, 26)
    if ema_12 is None or ema_26 is None:
        return None, None, None

    macd_line = ema_12 - ema_26
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


class TestMacdOptimization:
    def test_series_equals_prefix_recompute(self) -> None:
        # Deterministic pseudo-random closes — long enough for the signal EMA.
        closes = [100.0 + (i * 7919 % 211) / 7.0 - 15.0 for i in range(120)]
        assert _compute_macd(closes) == _reference_macd(closes)

    def test_short_series_unchanged(self) -> None:
        closes = [50.0 + i for i in range(26)]
        assert _compute_macd(closes) == _reference_macd(closes)

    def test_below_threshold_returns_none(self) -> None:
        assert _compute_macd([1.0] * 25) == (None, None, None)

    def test_ema_series_matches_ema_per_prefix(self) -> None:
        values = [i * 1.5 for i in range(40)]
        series = _ema_series(values, 9)
        for k in range(8, len(values)):
            assert series[k] == _ema(values[: k + 1], 9)


class TestCalendarAwareAnnualization:
    def test_stock_daily_series_lands_near_trading_days(self) -> None:
        """Weekday-only daily curve over a year → ~252, not the nominal 365."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        points: list[tuple[datetime, float]] = []
        day = start
        value = 100.0
        while day < start + timedelta(days=365):
            if day.weekday() < 5:  # Mon–Fri only
                value += 0.1
                points.append((day, value))
            day += timedelta(days=1)

        est = estimate_periods_per_year(points, nominal=365.0)
        assert 240.0 <= est <= 265.0  # realized cadence ≈ trading calendar

    def test_continuous_crypto_series_matches_nominal(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        points = [(start + timedelta(hours=h), 100.0 + h * 0.01) for h in range(24 * 90)]
        est = estimate_periods_per_year(points, nominal=8_760.0)
        assert 8_300.0 <= est <= 9_200.0

    def test_short_curve_falls_back_to_nominal(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        points = [(start + timedelta(minutes=5 * i), 100.0 + i) for i in range(20)]
        # 3h of event bursts: implausible cadence → nominal, never a wild estimate.
        assert estimate_periods_per_year(points, nominal=8_760.0) == 8_760.0

    def test_too_few_points_falls_back(self) -> None:
        assert estimate_periods_per_year([], 365.0) == 365.0


def test_sub_dollar_price_indicators_keep_significant_digits() -> None:
    """§7.59 L5: ATR/Bollinger of a 0.00004-priced asset must not round to 0.0."""
    from src.analysis.indicators import compute_indicators
    from src.core.models import OHLCV

    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = []
    for i in range(40):
        close = 0.00004 + (i % 5) * 0.000001
        candles.append(
            OHLCV(
                timestamp=start + timedelta(hours=i),
                open=close,
                high=close * 1.02,
                low=close * 0.98,
                close=close,
                volume=1_000_000.0,
            )
        )
    ind = compute_indicators(candles)
    for key in ("atr_14", "bb_upper", "bb_middle", "bb_lower"):
        assert ind[key] > 0, key
    assert ind["bb_middle"] == pytest.approx(0.000042, rel=0.05)
    assert ind["macd_line"] != 0.0


def test_large_price_indicators_keep_two_decimals() -> None:
    from src.analysis.indicators import _round_price

    assert _round_price(61_234.56789) == 61_234.57
    assert _round_price(12.345678, 4) == 12.3457
    assert _round_price(0.000123456789) == 0.000123457
    assert _round_price(0.0) == 0.0
