"""Tests for the Decision Pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.indicators import compute_indicators
from src.analysis.prompt_builder import build_user_prompt
from src.core.config import RiskSettings
from src.core.decision_pipeline import DecisionPipeline, calculate_quantity
from src.core.models import (
    OHLCV,
    Action,
    BookContext,
    DecisionRecord,
    Executor,
    MarketSnapshot,
    OrderSide,
    PortfolioState,
    Position,
    RiskResult,
    RiskVerdict,
    TradeSignal,
)
from src.core.risk_engine import RiskEngine
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
                realized_pnl=None,  # a HOLD never trades
            ),
        ]

        prompt = build_user_prompt(snapshot, prior)

        assert "outcome: +25.00 (win)" in prompt
        assert "outcome: -10.00 (loss)" in prompt
        # §7.45: a HOLD used to render "still open" forever — it never traded.
        assert "outcome: n/a (hold — no trade)" in prompt
        assert "still open" not in prompt.split("CONTEXT")[1].split("Each outcome")[0]

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

        # Cycle 2 — market drops to 96 (still above the 95 stop, which would now
        # auto-close it, §7.9); the cycle must re-mark the position.
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(98.0, 97.0, 96.0)
        )
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.HOLD,
            confidence=0.4,
            reasoning="ranging",
        )
        await pipeline.run(symbol="BTC/USDT")

        positions = await executor.get_positions()
        assert positions[0].current_price == pytest.approx(96.0)

        # The plan's acceptance check: unrealized PnL is no longer frozen at 0.
        portfolio = await pipeline.get_portfolio_state()
        assert portfolio.unrealized_pnl != 0.0
        assert portfolio.unrealized_pnl == pytest.approx(-40.0)  # 10 × (96 − 100)
        assert portfolio.total_value == pytest.approx(9_960.0)

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


class TestSizingAtTheGate:
    """Regression tests for [R-H2 / §7.5]: the order size must be computed
    *before* the risk check so the gate can reject an oversized plan, and sell
    sizing must never exceed the units actually held."""

    @staticmethod
    def _candles(*closes: float) -> list[OHLCV]:
        return [OHLCV(open=c, high=c + 1.0, low=c - 1.0, close=c, volume=1000.0) for c in closes]

    @pytest.mark.asyncio
    async def test_evaluate_receives_planned_notional(self, risk_settings: RiskSettings) -> None:
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(99.0, 100.0)
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.85,
            reasoning="bullish",
            stop_loss=95.0,
        )
        mock_risk = MagicMock()
        mock_risk.settings = risk_settings
        mock_risk.evaluate.return_value = RiskResult(verdict=RiskVerdict.APPROVED)

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=mock_risk,
            executor=executor,
        )
        result = await pipeline.run(symbol="BTC/USDT")

        # 10% of 10_000 at price 100 → 10 units → notional 1_000.
        kwargs = mock_risk.evaluate.call_args.kwargs
        assert kwargs["planned_notional"] == pytest.approx(1_000.0)
        # The plan the gate saw is exactly what gets executed — no re-sizing.
        placed_qty = (await executor.get_positions())[0].quantity
        assert placed_qty == pytest.approx(10.0)
        assert result.executed is True

    @pytest.mark.asyncio
    async def test_oversized_plan_rejected_end_to_end(self, risk_settings: RiskSettings) -> None:
        # A real risk engine with a deliberately regressed sizer: the gate must
        # catch it (this used to be invisible — nothing validated the size).
        from src.core.risk_engine import RiskEngine

        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(99.0, 100.0)
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.85,
            reasoning="bullish",
            stop_loss=95.0,
        )

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=RiskEngine(risk_settings),
            executor=executor,
        )
        # Sizing regression: 10× the intended notional (5_000 units at price 100).
        pipeline._calculate_quantity = MagicMock(return_value=5_000.0)  # type: ignore[method-assign]

        result = await pipeline.run(symbol="BTC/USDT")

        assert result.risk_result is not None
        assert result.risk_result.verdict == RiskVerdict.REJECTED
        assert "position" in (result.risk_result.reason or "").lower()
        assert result.order_result is None
        assert await executor.get_positions() == []

    @pytest.mark.asyncio
    async def test_sell_is_clamped_to_units_held(self, risk_settings: RiskSettings) -> None:
        from src.core.risk_engine import RiskEngine

        # Only 1 unit held, but the notional-based sizing would sell ~10.
        executor = PaperExecutor(initial_cash=9_900.0, slippage_pct=0.0)
        await executor.place_order("BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0)

        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=self._candles(99.0, 100.0)
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT",
            action=Action.SELL,
            confidence=0.85,
            reasoning="exit",
            stop_loss=110.0,
        )

        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=RiskEngine(risk_settings),
            executor=executor,
        )
        result = await pipeline.run(symbol="BTC/USDT")

        assert result.order_result is not None
        assert result.order_result.status == "filled"
        assert result.order_result.quantity == pytest.approx(1.0)
        assert await executor.get_positions() == []


class TestDecisionPersistence:
    """§7.8: the pipeline persists every decision (approved, rejected, HOLD and
    fallback alike) right after the risk gate, and exposes its row id on the
    result so orders can link back to it."""

    @staticmethod
    def _pipeline(
        risk_settings: RiskSettings,
        storage: object,
        signal: TradeSignal,
        candles: list[OHLCV],
    ) -> DecisionPipeline:
        from src.core.risk_engine import RiskEngine

        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=candles
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal.return_value = signal
        return DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=RiskEngine(risk_settings),
            executor=PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0),
            storage=storage,  # type: ignore[arg-type]
        )

    @staticmethod
    def _buy_signal() -> TradeSignal:
        return TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="momentum",
            stop_loss=95.0,
        )

    async def test_approved_decision_persisted_with_id(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV], tmp_db_path: str
    ) -> None:
        from src.core.storage import Storage

        store = Storage(tmp_db_path)
        await store.initialize()
        try:
            pipeline = self._pipeline(risk_settings, store, self._buy_signal(), sample_candles)
            result = await pipeline.run(symbol="BTC/USDT")

            assert result.executed
            assert result.decision_id is not None
            decisions = await store.get_recent_decisions("BTC/USDT")
            assert [d.id for d in decisions] == [result.decision_id]
            assert decisions[0].action == "buy"
            assert decisions[0].risk_verdict == "approved"
            # The cycle's market snapshot rides along with the decision.
            assert len(await store.get_recent_snapshots("BTC/USDT")) == 1
        finally:
            await store.close()

    async def test_rejected_decision_is_persisted_too(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV], tmp_db_path: str
    ) -> None:
        from src.core.storage import Storage

        store = Storage(tmp_db_path)
        await store.initialize()
        try:
            # No stop-loss → the gate rejects; the decision must still be on record.
            signal = TradeSignal(
                symbol="BTC/USDT", action=Action.BUY, confidence=0.9, reasoning="no risk plan"
            )
            pipeline = self._pipeline(risk_settings, store, signal, sample_candles)
            result = await pipeline.run(symbol="BTC/USDT")

            assert not result.executed
            assert result.decision_id is not None
            decisions = await store.get_recent_decisions("BTC/USDT")
            assert decisions[0].risk_verdict == "rejected"
        finally:
            await store.close()

    async def test_fallback_hold_persisted_but_excluded_from_context(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV], tmp_db_path: str
    ) -> None:
        import sqlite3

        from src.core.storage import Storage

        store = Storage(tmp_db_path)
        await store.initialize()
        try:
            fallback = TradeSignal(
                symbol="BTC/USDT",
                action=Action.HOLD,
                confidence=0.0,
                reasoning="LLM unavailable — safe fallback HOLD",
                is_fallback=True,
            )
            pipeline = self._pipeline(risk_settings, store, fallback, sample_candles)
            result = await pipeline.run(symbol="BTC/USDT")

            assert result.decision_id is not None
            # Stored for audit...
            conn = sqlite3.connect(tmp_db_path)
            try:
                stored = conn.execute("SELECT action, is_fallback FROM llm_decisions").fetchall()
            finally:
                conn.close()
            assert stored == [("hold", 1)]
            # ...but never re-fed into the next cycle's prompt context.
            assert await pipeline.get_recent_decisions("BTC/USDT") == []
        finally:
            await store.close()

    async def test_no_storage_still_runs_without_decision_id(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        pipeline = self._pipeline(risk_settings, None, self._buy_signal(), sample_candles)
        result = await pipeline.run(symbol="BTC/USDT")

        assert result.executed
        assert result.decision_id is None


class TestExitLevelEnforcement:
    """§7.9: when a position's mark price breaches the stop-loss / take-profit
    carried from its entry signal, the pipeline closes it on the next cycle —
    without calling the LLM and without the risk gate."""

    @staticmethod
    def _candles(price: float) -> list[OHLCV]:
        return [
            OHLCV(
                open=price,
                high=price + 1.0,
                low=price - 1.0,
                close=price,
                volume=1_000.0,
            )
            for _ in range(3)
        ]

    def _pipeline(
        self,
        risk_settings: RiskSettings,
        executor: PaperExecutor,
        prices: list[float],
        signals: list[TradeSignal],
    ) -> tuple[DecisionPipeline, AsyncMock]:
        from src.core.risk_engine import RiskEngine

        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot = AsyncMock(
            side_effect=[
                MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=self._candles(p))
                for p in prices
            ]
        )
        mock_llm = AsyncMock()
        mock_llm.ask_trade_signal = AsyncMock(side_effect=signals)
        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=mock_llm,
            risk_engine=RiskEngine(risk_settings),
            executor=executor,
        )
        return pipeline, mock_llm

    @staticmethod
    def _entry(stop_loss: float | None, take_profit: float | None) -> TradeSignal:
        return TradeSignal(
            symbol="BTC/USDT",
            action=Action.BUY,
            confidence=0.9,
            reasoning="enter",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    @staticmethod
    def _hold() -> TradeSignal:
        return TradeSignal(
            symbol="BTC/USDT", action=Action.HOLD, confidence=0.5, reasoning="sit out"
        )

    async def test_stop_loss_breach_closes_without_llm(self, risk_settings: RiskSettings) -> None:
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        pipeline, llm = self._pipeline(
            risk_settings,
            executor,
            prices=[100.0, 90.0],
            signals=[self._entry(stop_loss=95.0, take_profit=None), self._hold()],
        )

        first = await pipeline.run(symbol="BTC/USDT")
        assert first.executed
        positions = await executor.get_positions()
        assert positions[0].stop_loss == 95.0  # levels ride on the position (§7.9)

        second = await pipeline.run(symbol="BTC/USDT")
        assert second.auto_exit is True
        assert second.exit_reason == "stop_loss"
        assert second.executed
        assert second.order_result is not None
        assert second.order_result.side == OrderSide.SELL
        assert second.order_result.realized_pnl is not None
        assert second.order_result.realized_pnl < 0
        assert await executor.get_positions() == []
        # The LLM was asked once (cycle 1); the exit cycle never consults it,
        # so no new entry can be taken on top of the close.
        assert llm.ask_trade_signal.await_count == 1
        assert second.signal is None

    async def test_take_profit_breach_closes_with_profit(self, risk_settings: RiskSettings) -> None:
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        pipeline, _llm = self._pipeline(
            risk_settings,
            executor,
            prices=[100.0, 130.0],
            signals=[self._entry(stop_loss=95.0, take_profit=120.0), self._hold()],
        )

        await pipeline.run(symbol="BTC/USDT")
        second = await pipeline.run(symbol="BTC/USDT")

        assert second.auto_exit is True
        assert second.exit_reason == "take_profit"
        assert second.order_result is not None
        assert second.order_result.realized_pnl is not None
        assert second.order_result.realized_pnl > 0
        assert await executor.get_positions() == []

    async def test_no_breach_runs_the_normal_llm_path(self, risk_settings: RiskSettings) -> None:
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        pipeline, llm = self._pipeline(
            risk_settings,
            executor,
            prices=[100.0, 98.0],  # above the 95 stop
            signals=[self._entry(stop_loss=95.0, take_profit=None), self._hold()],
        )

        await pipeline.run(symbol="BTC/USDT")
        second = await pipeline.run(symbol="BTC/USDT")

        assert second.auto_exit is False
        assert llm.ask_trade_signal.await_count == 2

    async def test_enforcement_can_be_disabled(self) -> None:
        settings = RiskSettings(
            max_position_pct=0.10,
            daily_loss_limit_pct=0.02,
            max_drawdown_pct=0.05,
            consecutive_losses_cooldown_minutes=60,
            max_open_positions=5,
            min_confidence=0.6,
            enforce_exit_levels=False,
        )
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        pipeline, llm = self._pipeline(
            settings,
            executor,
            prices=[100.0, 90.0],  # would breach the stop if enforcement were on
            signals=[self._entry(stop_loss=95.0, take_profit=None), self._hold()],
        )

        await pipeline.run(symbol="BTC/USDT")
        second = await pipeline.run(symbol="BTC/USDT")

        assert second.auto_exit is False
        assert llm.ask_trade_signal.await_count == 2
        assert len(await executor.get_positions()) == 1

    async def test_exit_happens_even_while_the_gate_blocks_entries(
        self, risk_settings: RiskSettings
    ) -> None:
        """Cooldown must not strand a losing position: exits bypass the gate (§7.9)."""
        from src.core.risk_engine import RiskEngine

        engine = RiskEngine(risk_settings)
        for _ in range(3):  # trigger the consecutive-loss cooldown
            engine.record_outcome(was_profitable=False)
        assert engine._loss_tracker.in_cooldown is True

        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        mock_provider = AsyncMock()
        mock_provider.fetch_snapshot = AsyncMock(
            side_effect=[
                MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=self._candles(p))
                for p in (100.0, 90.0)
            ]
        )
        pipeline = DecisionPipeline(
            provider=mock_provider,
            llm_client=AsyncMock(),
            risk_engine=engine,
            executor=executor,
        )

        # The engine is in cooldown; a gate-checked order would be rejected.
        blocked = engine.evaluate(
            self._entry(stop_loss=95.0, take_profit=None), PortfolioState(cash=10_000.0)
        )
        assert blocked.verdict == RiskVerdict.REJECTED

        result = await pipeline._check_exit_levels(
            "BTC/USDT",
            MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=self._candles(90.0)),
        )
        # Nothing held yet → nothing to exit.
        assert result is None

        await executor.place_order(
            "BTC/USDT", OrderSide.BUY, quantity=1.0, price=100.0, stop_loss=95.0
        )
        closed = await pipeline._check_exit_levels(
            "BTC/USDT",
            MarketSnapshot(symbol="BTC/USDT", timeframe="1h", candles=self._candles(90.0)),
        )
        assert closed is not None
        assert closed.exit_reason == "stop_loss"
        assert closed.executed  # closed despite the cooldown blocking new entries


class TestPositionHeadroomSizing:
    """§7.42: sizing fills only the headroom under the per-position cap."""

    def test_buy_sized_to_remaining_headroom(self) -> None:
        rs = RiskSettings(max_position_pct=0.10)
        book = PortfolioState(
            cash=9_400.0,
            positions=[
                Position(
                    symbol="BTC/USDT", quantity=6.0, avg_entry_price=100.0, current_price=100.0
                )
            ],
        )  # total 10_000 → cap 1_000; 600 already held → 400 of headroom
        signal = TradeSignal(
            symbol="BTC/USDT", action=Action.BUY, confidence=0.9, reasoning="add", stop_loss=90.0
        )
        assert calculate_quantity(signal, book, rs, 100.0) == pytest.approx(4.0)
        full = signal.model_copy(update={"symbol": "ETH/USDT"})
        assert calculate_quantity(full, book, rs, 100.0) == pytest.approx(10.0)

    async def test_twelve_repeated_buys_stay_at_the_cap(self) -> None:
        """The review's reproduction (external_4 C4): 12 approved BUYs used to reach
        ~90% of equity in one symbol; now exposure stops at max_position_pct."""
        rs = RiskSettings()
        engine = RiskEngine(rs)
        executor = PaperExecutor(100_000.0, slippage_pct=0.001, fee_pct=0.0026)
        fills = 0
        for _ in range(12):
            book = PortfolioState(
                cash=await executor.get_cash(), positions=await executor.get_positions()
            )
            signal = TradeSignal(
                symbol="BTC/USDT", action=Action.BUY, confidence=0.9, reasoning="x", stop_loss=90.0
            )
            qty = calculate_quantity(signal, book, rs, 100.0)
            if engine.evaluate(signal, book, planned_notional=qty * 100.0).verdict == (
                RiskVerdict.APPROVED
            ):
                fills += (
                    await executor.place_order("BTC/USDT", OrderSide.BUY, qty, 100.0)
                ).status == "filled"
        final = PortfolioState(
            cash=await executor.get_cash(), positions=await executor.get_positions()
        )
        exposure = sum(p.quantity * p.current_price for p in final.positions)
        assert fills == 1
        assert exposure / final.total_value <= rs.max_position_pct * 1.01


class TestHonestOutcomeLabels:
    """§7.45: only an executed, unclosed entry is "still open"."""

    @staticmethod
    def _render(**fields) -> str:
        base = {"action": "buy", "confidence": 0.8, "reasoning": "r", "risk_verdict": "approved"}
        base.update(fields)
        snapshot = MarketSnapshot(symbol="BTC/USDT", timeframe="1h")
        return build_user_prompt(snapshot, [DecisionRecord(**base)])

    def test_rejected_entry_is_not_open(self) -> None:
        assert "outcome: n/a (rejected — not executed)" in self._render(risk_verdict="rejected")

    def test_unfilled_order_is_not_open(self) -> None:
        assert "outcome: n/a (order not filled)" in self._render(filled=False)

    def test_filled_entry_is_still_open(self) -> None:
        assert "outcome: still open" in self._render(filled=True)
        assert "outcome: still open" in self._render()  # fill status unknown → benefit of doubt

    def test_sell_without_outcome_is_na(self) -> None:
        assert "outcome: n/a (no tracked position closed)" in self._render(action="sell")


class TestBookSection:
    """§7.45: the prompt carries the agent's own position, cash and size limit."""

    @staticmethod
    def _snapshot() -> MarketSnapshot:
        return MarketSnapshot(symbol="BTC/USDT", timeframe="1h")

    def test_flat_book(self) -> None:
        book = BookContext(position=None, cash=10_000.0, total_value=10_000.0, max_position_pct=0.1)
        prompt = build_user_prompt(self._snapshot(), book=book)
        assert "YOUR BOOK" in prompt
        assert "Position in BTC/USDT: none (flat) — a SELL has nothing to close." in prompt
        assert "Cash: 10000.00 of total equity 10000.00" in prompt
        assert "room to add: 1000.00" in prompt

    def test_held_position_with_levels_and_limit(self) -> None:
        pos = Position(
            symbol="BTC/USDT",
            quantity=10.0,
            avg_entry_price=90.0,
            current_price=100.0,
            stop_loss=85.0,
            take_profit=120.0,
        )
        book = BookContext(position=pos, cash=9_000.0, total_value=10_000.0, max_position_pct=0.1)
        prompt = build_user_prompt(self._snapshot(), book=book)
        assert "LONG 10 @ avg 90.00, mark 100.00, unrealized +100.00 (+11.11%)" in prompt
        assert "10.0% of equity" in prompt
        assert "active stop 85.00, take-profit 120.00" in prompt
        assert "at the limit, a BUY will be rejected." in prompt

    def test_sub_dollar_prices_keep_precision(self) -> None:
        pos = Position(
            symbol="BTC/USDT", quantity=1000, avg_entry_price=0.1234, current_price=0.1301
        )
        book = BookContext(position=pos, cash=1_000.0, total_value=1_130.1)
        prompt = build_user_prompt(self._snapshot(), book=book)
        assert "avg 0.1234, mark 0.1301" in prompt

    async def test_pipeline_sends_the_book_to_the_llm(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        executor = PaperExecutor(initial_cash=10_000.0)
        await executor.place_order("BTC/USDT", OrderSide.BUY, 5.0, 100.0, stop_loss=90.0)
        provider = AsyncMock()
        provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=sample_candles
        )
        llm = AsyncMock()
        llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT", action=Action.HOLD, confidence=0.5, reasoning="wait"
        )
        pipeline = DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=RiskEngine(risk_settings),
            executor=executor,
        )

        await pipeline.run("BTC/USDT")

        prompt = llm.ask_trade_signal.call_args.kwargs["user_prompt"]
        assert "Position in BTC/USDT: LONG 5" in prompt
        assert "Position limit: 10% of equity per symbol" in prompt

    async def test_portfolio_read_failure_skips_the_llm_call(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        provider = AsyncMock()
        provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=sample_candles
        )
        executor = MagicMock()
        executor.get_positions = AsyncMock(return_value=[])
        executor.get_cash = AsyncMock(side_effect=RuntimeError("venue down"))
        llm = AsyncMock()
        pipeline = DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=RiskEngine(risk_settings),
            executor=executor,
        )

        result = await pipeline.run("BTC/USDT")

        assert result.error is not None and "Portfolio read failed" in result.error
        llm.ask_trade_signal.assert_not_awaited()


class TestHistoryFillLookup:
    """§7.45: prompt history knows which approved trades actually filled."""

    async def test_filled_flag_from_orders(self, tmp_path) -> None:
        from src.core.storage import Storage

        store = Storage(str(tmp_path / "h.db"), agent="crypto")
        await store.initialize()
        try:
            ids = []
            for _ in range(2):
                ids.append(
                    await store.save_llm_decision(
                        symbol="BTC/USDT",
                        action="buy",
                        confidence=0.8,
                        reasoning="r",
                        stop_loss=1.0,
                        take_profit=None,
                        risk_verdict="approved",
                        risk_reason=None,
                    )
                )
            await store.save_order("o1", "BTC/USDT", "buy", 1.0, 10.0, "filled", decision_id=ids[0])
            await store.save_order(
                "o2", "BTC/USDT", "buy", 1.0, 10.0, "rejected", decision_id=ids[1]
            )
            assert await store.get_filled_decision_ids(ids) == {ids[0]}

            pipeline = DecisionPipeline(
                provider=AsyncMock(),
                llm_client=AsyncMock(),
                risk_engine=MagicMock(),
                executor=PaperExecutor(),
                storage=store,
            )
            records = await pipeline.get_recent_decisions("BTC/USDT")
            assert [r.filled for r in records] == [False, True]  # newest first
        finally:
            await store.close()


class TestExitsCloseInFull:
    """§7.47: an approved SELL closes the whole long, even deep in drawdown/cooldown."""

    def test_sell_sizes_to_the_full_position(self) -> None:
        rs = RiskSettings(max_position_pct=0.10)
        book = PortfolioState(
            cash=1_000.0,
            positions=[
                Position(
                    symbol="BTC/USDT", quantity=50.0, avg_entry_price=90.0, current_price=100.0
                )
            ],
        )  # the position is 5000 of 6000 equity — far above a 10% "slice"
        signal = TradeSignal(symbol="BTC/USDT", action=Action.SELL, confidence=0.9, reasoning="x")
        assert calculate_quantity(signal, book, rs, 100.0) == pytest.approx(50.0)

    async def test_llm_exit_goes_through_during_cooldown(
        self, risk_settings: RiskSettings, sample_candles: list[OHLCV]
    ) -> None:
        engine = RiskEngine(risk_settings)
        for _ in range(3):
            engine.record_outcome(was_profitable=False)  # cooldown armed
        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        await executor.place_order("BTC/USDT", OrderSide.BUY, 5.0, 100.0)
        provider = AsyncMock()
        provider.fetch_snapshot.return_value = MarketSnapshot(
            symbol="BTC/USDT", timeframe="1h", candles=sample_candles
        )
        llm = AsyncMock()
        llm.ask_trade_signal.return_value = TradeSignal(
            symbol="BTC/USDT", action=Action.SELL, confidence=0.9, reasoning="get out"
        )
        pipeline = DecisionPipeline(
            provider=provider, llm_client=llm, risk_engine=engine, executor=executor
        )

        result = await pipeline.run("BTC/USDT")

        assert result.risk_result.verdict == RiskVerdict.APPROVED
        assert result.executed and result.order_result.quantity == pytest.approx(5.0)
        assert await executor.get_positions() == []
