"""Property-based tests for the deterministic risk engine (hypothesis).

These assert *invariants* that must hold for every input: the risk gate is the
safety-critical layer between the LLM and the exchange, so a single hand-picked
example per rule is not enough — we sweep the input space instead.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from src.core.config import RiskSettings
from src.core.models import Action, PortfolioState, Position, RiskVerdict, TradeSignal
from src.core.risk_engine import RiskEngine

# ── Strategies ────────────────────────────────────────────────

positive_money = st.floats(min_value=0.01, max_value=1e9, allow_nan=False, allow_infinity=False)
confidences = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
prices = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)


def make_settings() -> RiskSettings:
    """Risk settings within the ranges the shipped config uses."""
    return RiskSettings(
        max_position_pct=0.10,
        daily_loss_limit_pct=0.02,
        max_drawdown_pct=0.05,
        consecutive_losses_cooldown_minutes=60,
        max_open_positions=5,
        min_confidence=0.6,
    )


def make_portfolio(cash: float, extra_symbols: int) -> PortfolioState:
    positions = [
        Position(
            symbol=f"SYM{i}/USDT",
            quantity=1.0 + i,
            avg_entry_price=100.0,
            current_price=100.0,
        )
        for i in range(extra_symbols)
    ]
    return PortfolioState(cash=cash, positions=positions)


@given(
    confidence=confidences,
    cash=positive_money,
    extra_symbols=st.integers(min_value=0, max_value=12),
    stop_loss=st.one_of(st.none(), prices),
)
def test_hold_is_always_approved(confidence: float, cash: float, extra_symbols: int, stop_loss):
    """HOLD carries no exposure, so it must never be blocked — regardless of
    confidence, portfolio size or missing stops."""
    engine = RiskEngine(make_settings())
    signal = TradeSignal(
        symbol="BTC/USDT",
        action=Action.HOLD,
        confidence=confidence,
        reasoning="r",
        stop_loss=stop_loss,
    )
    result = engine.evaluate(signal, make_portfolio(cash, extra_symbols))
    assert result.verdict == RiskVerdict.APPROVED


@given(
    confidence=confidences,
    cash=positive_money,
    extra_symbols=st.integers(min_value=0, max_value=12),
)
def test_approved_entry_has_a_stop(confidence: float, cash: float, extra_symbols: int):
    """Any *approved* BUY must carry a stop-loss (the gate requires one for
    entries). Closes are exempt since §7.9."""
    engine = RiskEngine(make_settings())
    signal = TradeSignal(symbol="BTC/USDT", action=Action.BUY, confidence=confidence, reasoning="r")
    result = engine.evaluate(signal, make_portfolio(cash, extra_symbols))
    if result.verdict == RiskVerdict.APPROVED:
        assert signal.stop_loss is not None


@given(cash=positive_money)
def test_stopless_close_is_never_blocked_for_lacking_a_stop(cash: float):
    """A SELL without a stop must not be rejected *for lacking one* — exits reduce
    exposure, and blocking them strands the agent in losing positions (§7.9)."""
    engine = RiskEngine(make_settings())
    signal = TradeSignal(symbol="BTC/USDT", action=Action.SELL, confidence=0.9, reasoning="r")
    held = Position(symbol="BTC/USDT", quantity=1.0, avg_entry_price=100.0, current_price=100.0)
    result = engine.evaluate(signal, PortfolioState(cash=cash, positions=[held]))
    assert "stop-loss" not in (result.reason or "").lower()
    assert result.verdict == RiskVerdict.APPROVED


@given(
    action=st.sampled_from([Action.BUY, Action.SELL]),
    cash=positive_money,
    extra_symbols=st.integers(min_value=0, max_value=12),
)
def test_low_confidence_never_approved(action: Action, cash: float, extra_symbols: int):
    """Confidence below the minimum must be rejected for active signals."""
    engine = RiskEngine(make_settings())
    signal = TradeSignal(
        symbol="BTC/USDT",
        action=action,
        confidence=0.59,  # just below min_confidence=0.6
        reasoning="r",
        stop_loss=100.0,
    )
    result = engine.evaluate(signal, make_portfolio(cash, extra_symbols))
    assert result.verdict == RiskVerdict.REJECTED


@given(
    action=st.sampled_from([Action.BUY, Action.SELL]),
    cash=positive_money,
    extra_symbols=st.integers(min_value=5, max_value=12),
)
def test_new_position_at_max_never_approved(action: Action, cash: float, extra_symbols: int):
    """Opening a position beyond max_open_positions must be rejected."""
    engine = RiskEngine(make_settings())
    signal = TradeSignal(
        symbol="NEW/USDT",  # not in the portfolio → an opening trade
        action=action,
        confidence=1.0,
        reasoning="r",
        stop_loss=100.0,
    )
    result = engine.evaluate(signal, make_portfolio(cash, extra_symbols))
    assert result.verdict == RiskVerdict.REJECTED


@given(
    cash=positive_money,
    # Strictly beyond the -2% limit: exactly at the boundary float rounding can
    # land either side of the comparison, which is not what this property tests.
    decline=st.floats(min_value=0.03, max_value=0.9, allow_nan=False),
)
def test_daily_loss_limit_blocks_active_signals(cash: float, decline: float):
    """Once the daily loss breaches the limit, active signals are rejected."""
    engine = RiskEngine(make_settings())
    engine.update_daily_value(cash)  # sets today's baseline
    engine.update_daily_value(cash * (1.0 - decline))

    signal = TradeSignal(
        symbol="BTC/USDT",
        action=Action.BUY,
        confidence=1.0,
        reasoning="r",
        stop_loss=100.0,
    )
    result = engine.evaluate(signal, make_portfolio(cash * (1.0 - decline), 0))
    assert result.verdict == RiskVerdict.REJECTED


@given(action=st.sampled_from([Action.BUY]))
def test_three_losses_trigger_cooldown(action: Action):
    """Three consecutive losses must cool the engine down for *entries* (exits are
    never gated by the cooldown — §7.47, see ``test_exits_are_never_stranded``)."""
    engine = RiskEngine(make_settings())
    portfolio = make_portfolio(100_000.0, 0)
    signal = TradeSignal(
        symbol="BTC/USDT",
        action=action,
        confidence=1.0,
        reasoning="r",
        stop_loss=100.0,
    )
    assert engine.evaluate(signal, portfolio).verdict == RiskVerdict.APPROVED

    for _ in range(3):
        engine.record_outcome(was_profitable=False)

    result = engine.evaluate(signal, portfolio)
    assert result.verdict == RiskVerdict.REJECTED
    assert "cooldown" in (result.reason or "").lower()


@given(confidence=confidences, cash=positive_money)
@settings(max_examples=30)
def test_engine_is_deterministic(confidence: float, cash: float):
    """Same settings + same input ⇒ same verdict. The gate must never be random."""
    signal = TradeSignal(
        symbol="BTC/USDT",
        action=Action.BUY,
        confidence=confidence,
        reasoning="r",
        stop_loss=100.0,
        take_profit=200.0,
    )
    portfolio = make_portfolio(cash, 1)

    a = RiskEngine(make_settings()).evaluate(signal, portfolio)
    b = RiskEngine(make_settings()).evaluate(signal, portfolio)
    assert a.verdict == b.verdict


@settings(max_examples=150, deadline=None)
@given(
    prices=st.lists(
        st.floats(min_value=1.0, max_value=1e5, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=15,
    ),
    cash=st.floats(min_value=1_000.0, max_value=1e7, allow_nan=False, allow_infinity=False),
)
def test_repeated_buys_never_exceed_the_position_cap(prices: list[float], cash: float) -> None:
    """§7.42: however many BUYs the LLM repeats, the resulting position stays inside
    max_position_pct of equity (checked at each fill's price — the cap is an entry
    rule; later appreciation may legitimately lift it)."""
    import asyncio

    from src.core.decision_pipeline import calculate_quantity
    from src.core.models import OrderSide
    from src.execution.paper_executor import PaperExecutor

    rs = make_settings()
    engine = RiskEngine(rs)
    executor = PaperExecutor(initial_cash=cash, slippage_pct=0.0, fee_pct=0.0)

    async def run() -> None:
        for price in prices:
            executor.update_price("BTC/USDT", price)
            book = PortfolioState(
                cash=await executor.get_cash(), positions=await executor.get_positions()
            )
            signal = TradeSignal(
                symbol="BTC/USDT",
                action=Action.BUY,
                confidence=0.9,
                reasoning="again",
                stop_loss=price * 0.9,
            )
            qty = calculate_quantity(signal, book, rs, price)
            verdict = engine.evaluate(signal, book, planned_notional=qty * price)
            if verdict.verdict == RiskVerdict.APPROVED and qty > 0:
                await executor.place_order("BTC/USDT", OrderSide.BUY, qty, price)
            after = PortfolioState(
                cash=await executor.get_cash(), positions=await executor.get_positions()
            )
            held = sum(p.quantity * p.current_price for p in after.positions)
            if verdict.verdict == RiskVerdict.APPROVED:
                assert held <= rs.max_position_pct * after.total_value * (1 + 1e-6)

    asyncio.run(run())


@settings(max_examples=100, deadline=None)
@given(
    peak=st.floats(min_value=1_000.0, max_value=1e7, allow_nan=False, allow_infinity=False),
    drop=st.floats(min_value=0.0, max_value=0.99, allow_nan=False, allow_infinity=False),
    losses=st.integers(min_value=0, max_value=6),
    held_value_frac=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
)
def test_exits_are_never_stranded(peak: float, drop: float, losses: int, held_value_frac: float):
    """§7.47: however deep the drawdown, daily loss or losing streak, a confident SELL
    of a held long is approved — closing only reduces exposure."""
    engine = RiskEngine(make_settings())
    engine.seed_peak_equity(peak)
    engine.restore_daily_baseline(peak)
    for _ in range(losses):
        engine.record_outcome(was_profitable=False)
    equity = peak * (1.0 - drop)
    held = equity * held_value_frac
    portfolio = PortfolioState(
        cash=equity - held,
        positions=[
            Position(
                symbol="BTC/USDT", quantity=held / 100.0, avg_entry_price=100.0, current_price=100.0
            )
        ],
    )
    signal = TradeSignal(symbol="BTC/USDT", action=Action.SELL, confidence=0.9, reasoning="exit")
    assert engine.evaluate(signal, portfolio).verdict == RiskVerdict.APPROVED


@given(cash=positive_money)
def test_flat_sell_is_refused(cash: float):
    """§7.47: spot account — a SELL with no long position has nothing to close."""
    engine = RiskEngine(make_settings())
    signal = TradeSignal(symbol="BTC/USDT", action=Action.SELL, confidence=0.95, reasoning="r")
    result = engine.evaluate(signal, PortfolioState(cash=cash))
    assert result.verdict == RiskVerdict.REJECTED
    assert "No open long position" in (result.reason or "")
