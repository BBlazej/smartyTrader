"""Bar timing (§7.56): forming-bar handling and one LLM decision per closed bar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.analysis.candles import bar_close_time, split_forming, timeframe_delta
from src.analysis.indicators import compute_indicators
from src.analysis.prompt_builder import build_user_prompt
from src.core.config import RiskSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import OHLCV, Action, MarketSnapshot, TradeSignal
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor

NOW = datetime(2026, 9, 24, 12, 20, tzinfo=UTC)  # 20 minutes into the 12:00 bar


def hourly_candles(count: int = 40, end_open: datetime = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)):
    """``count`` hourly bars; the last one opens at ``end_open`` (forming at NOW)."""
    return [
        OHLCV(
            timestamp=end_open - timedelta(hours=count - 1 - i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=10.0,
        )
        for i in range(count)
    ]


class TestTimeframeHelpers:
    @pytest.mark.parametrize(
        ("tf", "expected"),
        [
            ("5m", timedelta(minutes=5)),
            ("1h", timedelta(hours=1)),
            ("4h", timedelta(hours=4)),
            ("1d", timedelta(days=1)),
            ("1w", timedelta(weeks=1)),
        ],
    )
    def test_known_timeframes(self, tf: str, expected: timedelta) -> None:
        assert timeframe_delta(tf) == expected

    @pytest.mark.parametrize("tf", ["", "1M", "hourly", "0h", "h1"])
    def test_unknown_timeframes_are_none(self, tf: str) -> None:
        assert timeframe_delta(tf) is None

    def test_split_detects_forming_last_bar(self) -> None:
        candles = hourly_candles()
        closed, forming = split_forming(candles, "1h", NOW)
        assert forming is candles[-1]
        assert closed == candles[:-1]

    def test_split_when_last_bar_closed(self) -> None:
        candles = hourly_candles(end_open=datetime(2026, 9, 24, 11, 0, tzinfo=UTC))
        closed, forming = split_forming(candles, "1h", NOW)
        assert forming is None and closed == candles

    def test_split_is_a_noop_without_timing(self) -> None:
        candles = hourly_candles()
        assert split_forming(candles, "weird", NOW) == (candles, None)
        untimed = [c.model_copy(update={"timestamp": None}) for c in candles]
        assert split_forming(untimed, "1h", NOW) == (untimed, None)

    def test_naive_timestamps_are_utc(self) -> None:
        candles = [
            c.model_copy(update={"timestamp": c.timestamp.replace(tzinfo=None)})
            for c in hourly_candles()
        ]
        _, forming = split_forming(candles, "1h", NOW)
        assert forming is candles[-1]

    def test_bar_close_time(self) -> None:
        bar = hourly_candles()[-2]  # opened 11:00
        assert bar_close_time(bar, "1h") == datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class TestFormingBarInPipelineAndPrompt:
    def test_prompt_labels_forming_bar(self) -> None:
        snap = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=hourly_candles(), fetched_at=NOW
        )
        prompt = build_user_prompt(snap)
        assert "Current price: 139.50 (live — current bar still forming)" in prompt
        assert prompt.count("[FORMING") == 1
        assert "2026-09-24T12:00:00+00:00" in prompt.split("[FORMING")[0].splitlines()[-1]

    async def test_indicators_use_closed_bars_only(self) -> None:
        candles = hourly_candles()
        # A wild forming bar must not move the indicators.
        candles[-1] = candles[-1].model_copy(update={"close": 10_000.0, "high": 10_000.0})
        pipeline, llm, _ = _pipeline(candles)
        result = await pipeline.run("BTC/USDT", timeframe="1h")
        assert result.snapshot.indicators == compute_indicators(candles[:-1])
        llm.ask_trade_signal.assert_awaited_once()


def _pipeline(candles, storage: Storage | None = None, new_bar_only: bool = False, clock=None):
    provider = AsyncMock()

    async def fetch(symbol: str, timeframe: str) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            candles=list(candles),
            fetched_at=clock() if clock else NOW,
        )

    provider.fetch_snapshot.side_effect = fetch
    llm = AsyncMock()
    llm.ask_trade_signal.return_value = TradeSignal(
        symbol="BTC/USDT", action=Action.HOLD, confidence=0.5, reasoning="wait"
    )
    executor = PaperExecutor(initial_cash=10_000.0)
    pipeline = DecisionPipeline(
        provider=provider,
        llm_client=llm,
        risk_engine=RiskEngine(RiskSettings()),
        executor=executor,
        storage=storage,
        decide_on_new_bar_only=new_bar_only,
    )
    return pipeline, llm, executor


class TestOneDecisionPerBar:
    async def test_same_bar_is_decided_once(self) -> None:
        pipeline, llm, _ = _pipeline(hourly_candles(), new_bar_only=True)
        first = await pipeline.run("BTC/USDT", timeframe="1h")
        second = await pipeline.run("BTC/USDT", timeframe="1h")
        assert first.skip_reason is None and first.signal is not None
        assert second.skip_reason is not None and "awaiting new 1h bar" in second.skip_reason
        assert llm.ask_trade_signal.await_count == 1

    async def test_new_closed_bar_asks_again(self) -> None:
        candles = hourly_candles()
        pipeline, llm, _ = _pipeline(candles, new_bar_only=True)
        await pipeline.run("BTC/USDT", timeframe="1h")
        # An hour later: the 12:00 bar has closed and 13:00 is forming.
        pipeline._last_decision_at["BTC/USDT"] = datetime(2026, 9, 24, 12, 20, tzinfo=UTC)
        later = hourly_candles(end_open=datetime(2026, 9, 24, 13, 0, tzinfo=UTC))
        pipeline.provider.fetch_snapshot.side_effect = lambda s, tf: MarketSnapshot(
            symbol=s, timeframe=tf, candles=later, fetched_at=NOW + timedelta(hours=1)
        )
        result = await pipeline.run("BTC/USDT", timeframe="1h")
        assert result.skip_reason is None
        assert llm.ask_trade_signal.await_count == 2

    async def test_fallback_hold_does_not_consume_the_bar(self) -> None:
        pipeline, llm, _ = _pipeline(hourly_candles(), new_bar_only=True)
        llm.ask_trade_signal.return_value = TradeSignal(
            symbol="X", action=Action.HOLD, confidence=0.0, reasoning="down", is_fallback=True
        )
        await pipeline.run("BTC/USDT", timeframe="1h")
        await pipeline.run("BTC/USDT", timeframe="1h")
        assert llm.ask_trade_signal.await_count == 2  # LLM outage → retried next cycle

    async def test_exit_levels_still_enforced_while_waiting(self) -> None:
        pipeline, llm, executor = _pipeline(hourly_candles(), new_bar_only=True)
        await pipeline.run("BTC/USDT", timeframe="1h")  # consumes the bar
        from src.core.models import OrderSide

        await executor.place_order("BTC/USDT", OrderSide.BUY, 1.0, 150.0, stop_loss=145.0)
        result = await pipeline.run("BTC/USDT", timeframe="1h")  # mark 139.5 < stop
        assert result.auto_exit and result.exit_reason == "stop_loss"
        assert llm.ask_trade_signal.await_count == 1

    async def test_restart_reads_last_decision_from_storage(self, tmp_path) -> None:
        store = Storage(str(tmp_path / "bars.db"), agent="crypto")
        await store.initialize()
        try:
            # Real wall clock: the stored decision is "now", after the last closed bar.
            real_now = datetime.now(UTC)
            open_forming = real_now.replace(minute=0, second=0, microsecond=0)
            candles = hourly_candles(end_open=open_forming)
            first, llm1, _ = _pipeline(
                candles, storage=store, new_bar_only=True, clock=lambda: real_now
            )
            await first.run("BTC/USDT", timeframe="1h")
            assert llm1.ask_trade_signal.await_count == 1

            restarted, llm2, _ = _pipeline(
                candles, storage=store, new_bar_only=True, clock=lambda: real_now
            )
            result = await restarted.run("BTC/USDT", timeframe="1h")
            assert result.skip_reason is not None
            llm2.ask_trade_signal.assert_not_awaited()
        finally:
            await store.close()

    async def test_off_by_default_asks_every_cycle(self) -> None:
        pipeline, llm, _ = _pipeline(hourly_candles())
        await pipeline.run("BTC/USDT", timeframe="1h")
        await pipeline.run("BTC/USDT", timeframe="1h")
        assert llm.ask_trade_signal.await_count == 2


class TestTimeframeConfig:
    def test_agent_config_defaults(self) -> None:
        from src.core.config import AgentConfig

        cfg = AgentConfig(enabled=True)
        assert cfg.timeframe is None  # runner applies the per-agent default
        assert cfg.decide_on_new_bar_only is True

    def test_shipped_settings_carry_timeframes(self) -> None:
        from src.core.config import Settings

        settings = Settings()
        assert settings.crypto_agent.timeframe == "1h"
        assert settings.stocks_agent.timeframe == "1d"
        assert settings.crypto_agent.decide_on_new_bar_only is True
