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
    Executor,
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
        # Sized generously (200 units) because the cycle now marks the position to
        # market before sizing, so the sell slice (10% of a higher total_value) can
        # exceed 100 units — sells are not clamped to units held (known §7.19).
        executor = PaperExecutor(initial_cash=100_000.0, slippage_pct=0.0)
        await executor.place_order(
            "BTC/USDT", OrderSide.BUY, quantity=200.0, price=last_close * 0.5
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


class TestPositionMarking:
    """Regression tests for [R-H1]: every cycle must re-mark open paper
    positions at the snapshot's last close **before** the risk check, so
    unrealized PnL, total value and the daily-loss rule track the market
    instead of a price frozen at entry."""

    @staticmethod
    def _candles(*closes: float) -> list[OHLCV]:
        return [OHLCV(open=c, high=c + 1.0, low=c - 1.0, close=c, volume=1000.0) for c in closes]

    @staticmethod
    def _approved_risk(risk_settings: RiskSettings) -> MagicMock:
        mock_risk = MagicMock()
        mock_risk.settings = risk_settings
        mock_risk.evaluate.return_value = RiskResult(verdict=RiskVerdict.APPROVED)
        return mock_risk

    @pytest.mark.asyncio
    async def test_cycle_re_marks_position_to_last_close(self, risk_settings: RiskSettings) -> None:
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        mock_provider = AsyncMock()
        mock_llm = AsyncMock()
        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=self._approved_risk(risk_settings),
            executor=executor,
        )

        # Cycle 1 — buy at close 100; the position is marked at its fill price.
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(98.0, 99.0, 100.0)
        )
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.85,
            reasoning="bullish",
            stop_loss=95.0,
        )
        await pipeline.run(symbol="BTC/USDT")

        positions = await executor.get_positions()
        assert len(positions) == 1
        assert positions[0].current_price == pytest.approx(100.0)
        # Sizing: 10% of 10_000 at price 100 → 10 units, cash 9_000.
        assert positions[0].quantity == pytest.approx(10.0)

        # Cycle 2 — market drops to 80; the cycle must re-mark the position.
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(85.0, 82.0, 80.0)
        )
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.HOLD,
            confidence=0.4,
            reasoning="ranging",
        )
        await pipeline.run(symbol="BTC/USDT")

        positions = await executor.get_positions()
        assert positions[0].current_price == pytest.approx(80.0)

        # The plan's acceptance check: unrealized PnL is no longer frozen at 0.
        portfolio = await pipeline._get_portfolio_state()
        assert portfolio.unrealized_pnl != 0.0
        assert portfolio.unrealized_pnl == pytest.approx(-200.0)  # 10 × (80 − 100)
        assert portfolio.total_value == pytest.approx(9_800.0)

    @pytest.mark.asyncio
    async def test_risk_check_sees_market_valued_portfolio(
        self, risk_settings: RiskSettings
    ) -> None:
        # The marking must happen BEFORE evaluate(), so the daily-loss rule
        # sees total_value at market — not the frozen entry valuation.
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=10.0, price=100.0)

        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(85.0, 82.0, 80.0)
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.HOLD,
            confidence=0.4,
            reasoning="ranging",
        )
        mock_risk = self._approved_risk(risk_settings)

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=mock_risk,
            executor=executor,
        )
        await pipeline.run(symbol="BTC/USDT")

        portfolio_arg = mock_risk.evaluate.call_args.args[1]
        assert portfolio_arg.total_value == pytest.approx(9_800.0)  # 9_000 cash + 10 × 80
        assert portfolio_arg.unrealized_pnl == pytest.approx(-200.0)

    @pytest.mark.asyncio
    async def test_marking_does_not_create_positions(self, risk_settings: RiskSettings) -> None:
        # A cycle on a symbol with no open position must not materialise one.
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="ETH/USDT", timeframe="1h", candles=self._candles(50.0, 49.0, 48.0)
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="ETH/USDT",
            action=Action.HOLD,
            confidence=0.5,
            reasoning="no edge",
        )

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=self._approved_risk(risk_settings),
            executor=executor,
        )
        await pipeline.run(symbol="ETH/USDT")

        assert await executor.get_positions() == []

    @pytest.mark.asyncio
    async def test_executor_without_marking_hook_is_skipped(
        self, risk_settings: RiskSettings
    ) -> None:
        # Real-venue executors (Kraken/XTB) report live prices and implement no
        # update_price hook — the pipeline must run unaffected.
        venue = AsyncMock(spec=Executor)
        venue.get_positions.return_value = []
        venue.get_cash.return_value = 10_000.0

        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(98.0, 99.0, 100.0)
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.HOLD,
            confidence=0.5,
            reasoning="no edge",
        )

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=self._approved_risk(risk_settings),
            executor=venue,
        )
        result = await pipeline.run(symbol="BTC/USDT")

        assert result.error is None
        assert result.signal is not None
