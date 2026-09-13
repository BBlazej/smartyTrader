"""Tests for the Decision Pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import RiskSettings
from src.core.decision_pipeline import (
    DecisionPipeline,
    build_user_prompt,
    compute_indicators,
)
from src.core.models import (
    OHLCV,
    Action,
    DecisionRecord,
    MarketSnapshot,
    OrderSide,
    RiskResult,
    RiskVerdict,
    TradeSignal,
)
from src.execution.paper_executor import PaperExecutor


@pytest.fixture()
def risk_settings() -> RiskSettings:
    return RiskSettings(
        max_position_pct=0.10,
        daily_loss_limit_pct=0.02,
        max_drawdown_pct=0.05,
        consecutive_losses_cooldown_minutes=60,
        max_open_positions=5,
        min_confidence=0.6,
    )


@pytest.fixture()
def sample_candles() -> list[OHLCV]:
    """Generate 30 sample candles for testing indicators and prompt building."""
    candles = []
    base_price = 100.0
    for i in range(30):
        price = base_price + (i % 5) - (i % 3)
        candles.append(
            OHLCV(
                open=price,
                high=price + 2.0,
                low=price - 1.0,
                close=price + 0.5,
                volume=1000.0 + i * 10,
            )
        )
    return candles


class TestComputeIndicators:
    def test_computes_all_indicators(self, sample_candles: list[OHLCV]) -> None:
        indicators = compute_indicators(sample_candles)

        assert "rsi_14" in indicators
        assert "macd_line" in indicators
        assert "macd_signal" in indicators
        assert "macd_histogram" in indicators
        assert "bb_upper" in indicators
        assert "bb_middle" in indicators
        assert "bb_lower" in indicators
        assert "bb_bandwidth" in indicators
        assert "atr_14" in indicators
        assert "volume_sma_20" in indicators

        assert 0 <= indicators["rsi_14"] <= 100

    def test_empty_candles_returns_empty(self) -> None:
        assert compute_indicators([]) == {}


class TestBuildUserPrompt:
    def test_builds_prompt_with_indicators(self, sample_candles: list[OHLCV]) -> None:
        snapshot = MarketSnapshot(
            symbol="BTC/USDT",
            timeframe="1h",
            candles=sample_candles,
            indicators={"rsi_14": 55.4, "macd_line": 1.2},
        )

        prompt = build_user_prompt(snapshot)
        assert "Symbol: BTC/USDT (1h)" in prompt
        assert "rsi_14: 55.4" in prompt
        assert "OHLCV" in prompt

    def test_no_context_section_without_prior_decisions(self, sample_candles: list[OHLCV]) -> None:
        snapshot = MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=sample_candles)
        prompt = build_user_prompt(snapshot)
        assert "CONTEXT" not in prompt

    def test_renders_prior_decisions_context(self, sample_candles: list[OHLCV]) -> None:
        snapshot = MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=sample_candles)
        prior = [
            DecisionRecord(
                action="buy",
                confidence=0.85,
                reasoning="Strong bullish momentum",
                risk_verdict="approved",
                risk_reason=None,
            ),
            DecisionRecord(
                action="sell",
                confidence=0.7,
                reasoning="Trend exhaustion",
                risk_verdict="rejected",
                risk_reason="Daily loss limit reached",
            ),
        ]

        prompt = build_user_prompt(snapshot, prior)

        assert "CONTEXT — your most recent decisions" in prompt
        assert "action: buy" in prompt
        assert "confidence: 0.85" in prompt
        assert "risk verdict: approved" in prompt
        assert "action: sell" in prompt
        assert "risk reason: Daily loss limit reached" in prompt
        assert "Strong bullish momentum" in prompt
        # Most recent first — buy (index 0) appears before sell.
        assert prompt.index("action: buy") < prompt.index("action: sell")

    def test_renders_outcome_in_context(self, sample_candles: list[OHLCV]) -> None:
        snapshot = MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=sample_candles)
        prior = [
            DecisionRecord(
                action="buy",
                confidence=0.85,
                reasoning="Strong bullish momentum",
                risk_verdict="approved",
                realized_pnl=25.0,  # a win
            ),
            DecisionRecord(
                action="sell",
                confidence=0.7,
                reasoning="Trend exhaustion",
                risk_verdict="approved",
                realized_pnl=-10.0,  # a loss
            ),
            DecisionRecord(
                action="hold",
                confidence=0.6,
                reasoning="No edge",
                risk_verdict="approved",
                realized_pnl=None,  # position still open
            ),
        ]

        prompt = build_user_prompt(snapshot, prior)

        assert "outcome: +25.00 (win)" in prompt
        assert "outcome: -10.00 (loss)" in prompt
        assert "outcome: still open" in prompt

    def test_outcome_flat_renders_zero(self, sample_candles: list[OHLCV]) -> None:
        snapshot = MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=sample_candles)
        prior = [
            DecisionRecord(
                action="sell",
                confidence=0.7,
                reasoning="Breakeven exit",
                risk_verdict="approved",
                realized_pnl=0.0,  # flat
            ),
        ]

        prompt = build_user_prompt(snapshot, prior)

        assert "outcome: 0.00 (flat)" in prompt


class TestDecisionPipelineRun:
    @pytest.mark.asyncio
    async def test_successful_buy_execution(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        # Provider mock
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT",
            timeframe="1h",
            candles=sample_candles,
        )

        # LLM client mock
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.85,
            reasoning="Strong bullish momentum",
            stop_loss=95.0,
            take_profit=110.0,
        )

        # Risk engine mock
        mock_risk = MagicMock()
        mock_risk.settings = risk_settings
        mock_risk.evaluate.return_value = RiskResult(verdict=RiskVerdict.APPROVED)

        # Paper executor
        executor = PaperExecutor(initial_cash=10000.0)

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=mock_risk,
            executor=executor,
        )

        result = await pipeline.run(symbol="BTC/USDT", timeframe="1h")

        assert result.symbol == "BTC/USDT"
        assert result.signal is not None
        assert result.signal.action == Action.BUY
        assert result.risk_result is not None
        assert result.risk_result.verdict == RiskVerdict.APPROVED
        assert result.order_result is not None
        assert result.order_result.status == "filled"
        assert result.executed is True

    @pytest.mark.asyncio
    async def test_hold_signal_does_not_execute(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="ETH/USDT",
            timeframe="1h",
            candles=sample_candles,
        )

        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="ETH/USDT",
            action=Action.HOLD,
            confidence=0.5,
            reasoning="Market ranging",
        )

        mock_risk = MagicMock()
        mock_risk.settings = risk_settings
        mock_risk.evaluate.return_value = RiskResult(verdict=RiskVerdict.APPROVED)

        executor = PaperExecutor(initial_cash=10000.0)

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=mock_risk,
            executor=executor,
        )

        result = await pipeline.run(symbol="ETH/USDT", timeframe="1h")

        assert result.signal.action == Action.HOLD
        assert result.order_result is None
        assert result.executed is False

    @pytest.mark.asyncio
    async def test_risk_rejection_prevents_order(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="SOL/USDT",
            timeframe="1h",
            candles=sample_candles,
        )

        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="SOL/USDT",
            action=Action.BUY,
            confidence=0.4,  # Low confidence
            reasoning="Weak signal",
            stop_loss=90.0,
        )

        mock_risk = MagicMock()
        mock_risk.settings = risk_settings
        mock_risk.evaluate.return_value = RiskResult(
            verdict=RiskVerdict.REJECTED, reason="Low confidence"
        )

        executor = PaperExecutor(initial_cash=10000.0)

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=mock_risk,
            executor=executor,
        )

        result = await pipeline.run(symbol="SOL/USDT", timeframe="1h")

        assert result.risk_result.verdict == RiskVerdict.REJECTED
        assert result.order_result is None
        assert result.executed is False

    @pytest.mark.asyncio
    async def test_fetch_error_handled(self, risk_settings: RiskSettings) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.side_effect = Exception("API offline")

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=AsyncMock(),
            risk_engine=MagicMock(),
            executor=PaperExecutor(),
        )

        result = await pipeline.run(symbol="BTC/USDT", timeframe="1h")

        assert result.error is not None
        assert "Fetch failed" in result.error
        assert result.signal is None
        assert result.executed is False


class TestOutcomeRecording:
    """Regression tests: the risk engine must only record a true win/loss.

    A win/loss is knowable only once a position is actually closed. The
    pipeline must never fabricate a win at fill time (a buy), and must record
    the correct sign once a sell realizes PnL.
    """

    @staticmethod
    def _approved_risk(risk_settings: RiskSettings) -> MagicMock:
        mock_risk = MagicMock()
        mock_risk.settings = risk_settings
        mock_risk.evaluate.return_value = RiskResult(verdict=RiskVerdict.APPROVED)
        return mock_risk

    @pytest.mark.asyncio
    async def test_buy_does_not_record_outcome(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=sample_candles
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.85,
            reasoning="bullish",
            stop_loss=95.0,
        )
        mock_risk = self._approved_risk(risk_settings)
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=mock_risk,
            executor=executor,
        )
        result = await pipeline.run(symbol="BTC/USDT")

        assert result.order_result is not None
        assert result.order_result.status == "filled"
        # A buy has no realized PnL — the loss tracker must not be touched.
        mock_risk.record_outcome.assert_not_called()

    @pytest.mark.asyncio
    async def test_winning_sell_records_profit(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        last_close = sample_candles[-1].close

        # Pre-open a position well below the current close so a pipeline sell is a win.
        executor = PaperExecutor(initial_cash=100_000.0, slippage_pct=0.0)
        await executor.place_order(
            "BTC/USDT", OrderSide.BUY, quantity=100.0, price=last_close * 0.5
        )

        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=sample_candles
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.SELL,
            confidence=0.85,
            reasoning="take profit",
            stop_loss=last_close * 1.1,
        )
        mock_risk = self._approved_risk(risk_settings)

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=mock_risk,
            executor=executor,
        )
        result = await pipeline.run(symbol="BTC/USDT")

        assert result.order_result is not None
        assert result.order_result.status == "filled"
        assert result.order_result.realized_pnl is not None
        assert result.order_result.realized_pnl > 0
        mock_risk.record_outcome.assert_called_once_with(was_profitable=True)


class TestGetRecentDecisions:
    """Decision-history context: fetch prior decisions for prompt injection.

    Must be fail-soft — a storage error or missing storage must never break a
    trading cycle, so the pipeline returns an empty list in every degraded case.
    """

    def _make_row(self, action: str = "buy") -> MagicMock:
        row = MagicMock()
        row.action = action
        row.confidence = 0.9
        row.reasoning = "test reasoning"
        row.risk_verdict = "approved"
        row.risk_reason = None
        row.timestamp = None
        return row

    def _pipeline(self, storage: object | None, limit: int = 10) -> DecisionPipeline:
        return DecisionPipeline(
            provider=AsyncMock(),
            llm_client=AsyncMock(),
            risk_engine=MagicMock(),
            executor=PaperExecutor(),
            storage=storage,  # type: ignore[arg-type]
            decision_history_limit=limit,
        )

    @pytest.mark.asyncio
    async def test_returns_converted_records(self) -> None:
        rows = [self._make_row("buy"), self._make_row("sell")]
        storage = MagicMock()
        storage.get_recent_decisions = AsyncMock(return_value=rows)

        records = await self._pipeline(storage).get_recent_decisions("BTC/USDT")

        assert [r.action for r in records] == ["buy", "sell"]
        assert all(isinstance(r, DecisionRecord) for r in records)
        storage.get_recent_decisions.assert_awaited_once_with("BTC/USDT", limit=10)

    @pytest.mark.asyncio
    async def test_empty_when_no_storage(self) -> None:
        assert await self._pipeline(None).get_recent_decisions("BTC/USDT") == []

    @pytest.mark.asyncio
    async def test_empty_when_limit_zero(self) -> None:
        storage = MagicMock()
        storage.get_recent_decisions = AsyncMock(return_value=[self._make_row()])

        # limit=0 disables the section entirely — no DB call at all.
        assert await self._pipeline(storage, limit=0).get_recent_decisions("BTC/USDT") == []
        storage.get_recent_decisions.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_when_storage_raises(self) -> None:
        storage = MagicMock()
        storage.get_recent_decisions = AsyncMock(side_effect=Exception("db down"))

        # Must not propagate — a context-fetch failure must not break a cycle.
        assert await self._pipeline(storage).get_recent_decisions("BTC/USDT") == []
