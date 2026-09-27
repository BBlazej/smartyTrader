"""Unit tests for the deterministic screener (§7.70)."""

from __future__ import annotations

import math

import pytest

from src.analysis.screener import (
    ScreenMetrics,
    compute_screen_metrics,
    filter_by_liquidity,
    rank_candidates,
)
from src.core.models import OHLCV


def _closes_to_candles(closes: list[float], volume: float = 100.0) -> list[OHLCV]:
    return [OHLCV(open=c, high=c * 1.01, low=c * 0.99, close=c, volume=volume) for c in closes]


class TestFilterByLiquidity:
    def test_floor_drops_thin_and_unmeasurable_symbols(self) -> None:
        volumes = {"A/EUR": 2_000_000.0, "B/EUR": 500_000.0, "C/EUR": 0.0}
        kept = filter_by_liquidity(volumes, 1_000_000.0)
        assert kept == [("A/EUR", 2_000_000.0)]

    def test_sorted_volume_descending_then_symbol(self) -> None:
        volumes = {"Z/EUR": 5.0, "A/EUR": 5.0, "M/EUR": 9.0}
        kept = filter_by_liquidity(volumes, 1.0)
        assert [symbol for symbol, _ in kept] == ["M/EUR", "A/EUR", "Z/EUR"]

    def test_missing_volume_is_never_a_candidate(self) -> None:
        volumes = {"A/EUR": None, "B/EUR": 3_000_000.0}  # type: ignore[dict-item]
        assert filter_by_liquidity(volumes, 1_000_000.0) == [("B/EUR", 3_000_000.0)]


class TestComputeScreenMetrics:
    def test_momentum_volatility_and_spike_on_synthetic_series(self) -> None:
        # Flat closes ⇒ zero volatility; last day's volume is double the mean.
        closes = [100.0] * 20
        candles = _closes_to_candles(closes[:-1], volume=100.0) + [
            OHLCV(open=100, high=100, low=100, close=100.0, volume=200.0)
        ]
        metrics = compute_screen_metrics(candles, momentum_days=5, lookback_days=19)
        assert metrics is not None
        assert metrics.momentum == pytest.approx(0.0)
        assert metrics.daily_volatility == pytest.approx(0.0)
        assert metrics.volume_spike == pytest.approx(200.0 / 100.0)

    def test_momentum_window_is_the_last_n_closes(self) -> None:
        # The old 90 sits just outside the momentum reference: using it would give
        # 110/90-1 = 22 %, the correct window gives exactly 10 %.
        closes = [90.0] + [100.0] * 14 + [110.0]
        metrics = compute_screen_metrics(
            _closes_to_candles(closes), momentum_days=14, lookback_days=15
        )
        assert metrics is not None
        assert metrics.momentum == pytest.approx(0.1)

    def test_too_short_series_returns_none_never_fabricated_metrics(self) -> None:
        assert compute_screen_metrics(_closes_to_candles([1.0] * 5), 14, 30) is None

    def test_nonpositive_reference_close_returns_none(self) -> None:
        # The zero sits exactly at the momentum reference position (6th from end).
        closes = [1.0] * 14 + [0.0] + [1.0] * 5
        assert compute_screen_metrics(_closes_to_candles(closes), 5, 19) is None

    def test_invalid_window_params_raise(self) -> None:
        with pytest.raises(ValueError):
            compute_screen_metrics([], momentum_days=0, lookback_days=30)
        with pytest.raises(ValueError):
            compute_screen_metrics([], momentum_days=5, lookback_days=1)


class TestRankCandidates:
    @staticmethod
    def _triple(symbol: str, momentum: float, vol: float, volume: float = 2e6):
        return (
            symbol,
            volume,
            ScreenMetrics(momentum=momentum, daily_volatility=vol, volume_spike=None),
        )

    def test_volatility_band_excludes_flat_and_blowup_instruments(self) -> None:
        ranked = rank_candidates(
            [
                self._triple("FLAT/EUR", 0.5, 0.001),
                self._triple("GOOD/EUR", 0.2, 0.02),
                self._triple("BLOWUP/EUR", 0.9, 0.60),
            ],
            min_daily_volatility=0.005,
            max_daily_volatility=0.25,
        )
        assert [c.symbol for c in ranked] == ["GOOD/EUR"]
        assert ranked[0].rank == 1

    def test_ranked_by_momentum_with_deterministic_tiebreaks(self) -> None:
        ranked = rank_candidates(
            [
                self._triple("B/EUR", 0.10, 0.02, volume=1e6),
                self._triple("A/EUR", 0.10, 0.02, volume=9e6),
                self._triple("C/EUR", 0.30, 0.02),
            ]
        )
        assert [c.symbol for c in ranked] == ["C/EUR", "A/EUR", "B/EUR"]
        assert [c.rank for c in ranked] == [1, 2, 3]

    def test_no_upper_band_when_none(self) -> None:
        ranked = rank_candidates([self._triple("WILD/EUR", 0.1, 5.0)], max_daily_volatility=None)
        assert len(ranked) == 1
        assert math.isfinite(ranked[0].metrics.daily_volatility)
