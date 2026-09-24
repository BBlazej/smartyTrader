"""Two agents, one SQLite file (§7.39, external review 4 C1).

Docker-compose runs the crypto and stocks runners against the same database. Before
§7.39 the per-agent tables carried no owner, so a crypto restart rehydrated the stocks
book, the drawdown peak was shared, and one agent's losing streak armed the other's
cooldown. These tests replay that exact scenario through the real rehydration path.
"""

from __future__ import annotations

import json

import pytest

from src.core.config import RiskSettings
from src.core.models import Action, OrderSide, PortfolioState, RiskVerdict, TradeSignal
from src.core.rehydration import rehydrate_from_storage
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor


def _book(symbol: str, qty: float, price: float) -> str:
    return json.dumps(
        [{"symbol": symbol, "quantity": qty, "avg_entry_price": price, "current_price": price}]
    )


@pytest.fixture()
async def dbs(tmp_path):
    path = str(tmp_path / "shared.db")
    crypto = Storage(path, agent="crypto")
    stocks = Storage(path, agent="stocks")
    await crypto.initialize()
    yield crypto, stocks
    await crypto.close()
    await stocks.close()


async def _restart(storage: Storage) -> tuple[PaperExecutor, RiskEngine]:
    """What ``run_agent`` does at startup: seed the peak, then rehydrate."""
    engine = RiskEngine(RiskSettings())
    engine.seed_peak_equity(await storage.get_max_portfolio_value())
    executor = PaperExecutor(initial_cash=100_000.0)
    await rehydrate_from_storage(engine, executor, storage)
    return executor, engine


class TestSharedDatabase:
    async def test_restart_restores_own_book_not_the_last_writer(self, dbs) -> None:
        crypto, stocks = dbs
        await crypto.save_portfolio_snapshot(
            cash=90_000.0, positions_json=_book("BTC/USDT", 0.1, 100_000.0), total_value=100_000.0
        )
        # The stocks agent writes *after* crypto — pre-§7.39 crypto adopted this book.
        await stocks.save_portfolio_snapshot(
            cash=50_000.0, positions_json=_book("AAPL", 200, 300.0), total_value=110_000.0
        )

        executor, _ = await _restart(crypto)

        assert await executor.get_cash() == 90_000.0
        assert [p.symbol for p in await executor.get_positions()] == ["BTC/USDT"]

    async def test_drawdown_peak_is_per_agent(self, dbs) -> None:
        crypto, stocks = dbs
        await crypto.save_portfolio_snapshot(
            cash=100_000.0, positions_json="[]", total_value=100_000.0
        )
        await stocks.save_portfolio_snapshot(
            cash=110_000.0, positions_json="[]", total_value=110_000.0
        )

        _, engine = await _restart(crypto)

        assert engine.peak_equity == 100_000.0
        signal = TradeSignal(
            symbol="BTC/USDT", action=Action.BUY, confidence=0.9, reasoning="x", stop_loss=1.0
        )
        verdict = engine.evaluate(signal, PortfolioState(cash=100_000.0), planned_notional=1_000.0)
        # Pre-§7.39: "Drawdown 9.09% exceeds limit 5.00% (peak equity 110000.00)".
        assert verdict.verdict == RiskVerdict.APPROVED

    async def test_fill_replay_and_loss_streak_stay_per_agent(self, dbs) -> None:
        crypto, stocks = dbs
        await crypto.save_portfolio_snapshot(
            cash=99_000.0, positions_json=_book("BTC/USDT", 0.01, 100_000.0), total_value=100_000.0
        )
        buy_id = await crypto.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="entry",
            stop_loss=90_000.0,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        await crypto.save_order(
            "c-buy", "BTC/USDT", "buy", 0.01, 100_000.0, "filled", decision_id=buy_id
        )
        # Stocks: a fill of its own plus a 3-loss streak that must not cool crypto down.
        await stocks.save_order("s-buy", "AAPL", "buy", 10, 300.0, "filled")
        for _ in range(3):
            await stocks.save_llm_decision(
                symbol="AAPL",
                action="sell",
                confidence=0.8,
                reasoning="loss",
                stop_loss=None,
                take_profit=None,
                risk_verdict="approved",
                risk_reason=None,
                realized_pnl=-10.0,
            )

        executor, engine = await _restart(crypto)

        # The crypto ledger holds exactly its own lot, with its entry decision id.
        outcome = await executor.place_order(
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            quantity=0.01,
            price=110_000.0,
        )
        assert [e.entry_decision_id for e in outcome.closed_entries] == [buy_id]
        assert "AAPL" not in {p.symbol for p in await executor.get_positions()}
        assert (
            engine.evaluate(
                TradeSignal(
                    symbol="ETH/USDT",
                    action=Action.BUY,
                    confidence=0.9,
                    reasoning="x",
                    stop_loss=1.0,
                ),
                PortfolioState(cash=100_000.0),
                planned_notional=1_000.0,
            ).verdict
            == RiskVerdict.APPROVED
        )
