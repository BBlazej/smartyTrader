"""Tests for the LLM latency benchmark CLI (§7.69). Mocked provider + LLM only."""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.benchmark_llm import _percentile, run
from src.core.llm_client import LLMCallMetrics
from src.core.models import OHLCV, Action, MarketSnapshot, TradeSignal


def test_percentile_nearest_rank() -> None:
    values = [float(i * 100) for i in range(1, 11)]  # 100..1000
    assert _percentile(values, 0.50) == 500.0
    assert _percentile(values, 0.95) == 1000.0
    assert _percentile([], 0.5) is None


class FakeLLM:
    """Deterministic LLMClient stand-in with scripted latencies."""

    def __init__(self, latencies_ms: list[float]) -> None:
        self._latencies = latencies_ms
        self.calls = 0
        self.last_metrics: LLMCallMetrics | None = None

    async def ask_trade_signal(self, *, system_prompt: str, user_prompt: str) -> TradeSignal:
        latency = self._latencies[min(self.calls, len(self._latencies) - 1)]
        self.calls += 1
        self.last_metrics = LLMCallMetrics(
            latency_ms=latency, prompt_tokens=1000, completion_tokens=200, attempts=1
        )
        return TradeSignal(symbol="BTC/EUR", action=Action.HOLD, confidence=0.7, reasoning="x")

    async def close(self) -> None:
        return None


def _snapshot() -> MarketSnapshot:
    candles = [
        OHLCV(open=100 + i, high=102 + i, low=99 + i, close=101 + i, volume=1000.0)
        for i in range(40)
    ]
    return MarketSnapshot(symbol="BTC/EUR", timeframe="1h", candles=candles)


def _args(**overrides: object) -> argparse.Namespace:
    base = {
        "symbols": ["BTC/EUR"],
        "timeframe": None,
        "repeats": 3,
        "warmup": 1,
        "provider": "ccxt",
        "budget": 0.5,
        "report": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


async def test_run_reports_percentiles_excluding_warmup() -> None:
    # 3 rounds × 1 symbol: latencies 9_999 / 2_000 / 4_000 — the warmup round must not
    # contaminate the percentiles.
    fake = FakeLLM([9_999.0, 2_000.0, 4_000.0])
    provider = AsyncMock()
    provider.fetch_snapshot.return_value = _snapshot()

    with (
        patch("src.data.ccxt_provider.create_ccxt_provider", return_value=provider),
        patch("scripts.benchmark_llm.LLMClient", return_value=fake),
    ):
        report = await run(_args())

    assert report["fallbacks"] == 0
    lat = report["latency_ms"]
    assert lat["count"] == 2  # warmup round excluded
    assert lat["p50"] == pytest.approx(2_000.0)
    assert lat["max"] == pytest.approx(4_000.0)
    assert report["tokens"]["avg_completion"] == pytest.approx(200.0)
    # The warmup sample is kept (flagged) for transparency.
    assert [s["warmup"] for s in report["samples"]] == [True, False, False]


async def test_run_counts_fallbacks_and_survives(capsys) -> None:
    fake = MagicMock()
    fake.last_metrics = LLMCallMetrics(1.0, None, None, 1)
    fake.close = AsyncMock()

    async def _fallback(**kwargs: object) -> TradeSignal:
        return TradeSignal(
            symbol="BTC/EUR", action=Action.HOLD, confidence=0.0, reasoning="down", is_fallback=True
        )

    fake.ask_trade_signal = AsyncMock(side_effect=_fallback)
    provider = AsyncMock()
    provider.fetch_snapshot.return_value = _snapshot()
    with (
        patch("src.data.ccxt_provider.create_ccxt_provider", return_value=provider),
        patch("scripts.benchmark_llm.LLMClient", return_value=fake),
    ):
        report = await run(_args(repeats=1, warmup=0))
    assert report["fallbacks"] == 1


def test_main_rejects_bad_flags() -> None:
    from scripts.benchmark_llm import main

    with patch("sys.argv", ["benchmark_llm", "--repeats", "0"]), pytest.raises(SystemExit):
        main()
    with (
        patch("sys.argv", ["benchmark_llm", "--repeats", "2", "--warmup", "2"]),
        pytest.raises(SystemExit),
    ):
        main()
