"""Per-sleeve books and risk (§7.71 step 2, CHANGE.md §4.8).

Each sleeve is measured on its own capital (weight × the allocation's cost-basis
equity + its own realized PnL since + unrealized PnL of the positions it owns) and
gated by its own RiskEngine; one loose agent-wide backstop sits on top. These tests
pin the accounting, the per-sleeve gates, outcome routing, and restart rehydration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.agents.crypto_agent import CryptoAgent
from src.analysis.prompt_builder import system_prompt_for
from src.core.config import RiskSettings, SleevesSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import Action, OrderSide, PortfolioState, TradeSignal
from src.core.risk_engine import RiskEngine
from src.core.sleeves import SleeveBook, SleeveRun, sleeve_risk_settings
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor
from tests.unit.test_sleeves import FakeProvider, _llm

AGENT_RISK = RiskSettings(max_position_pct=0.1, max_drawdown_pct=0.05, max_open_positions=5)


def _cfg(swing_risk: dict | None = None, weights: tuple[float, float] = (0.5, 0.5)):
    return SleevesSettings(
        enabled=True,
        backstop_max_drawdown_pct=0.20,
        strategies={
            "swing": {
                "timeframe": "1h",
                "holding": {"max_hours": 72},
                "weight": weights[0],
                "risk": {"max_drawdown_pct": 0.06, **(swing_risk or {})},
            },
            "position": {
                "timeframe": "4h",
                "playbook": "position",
                "weight": weights[1],
                "risk": {"max_drawdown_pct": 0.15, "max_stop_distance_pct": 0.20},
            },
        },
    )


@pytest.fixture()
async def storage(tmp_path: Path):
    store = Storage(str(tmp_path / "books.db"), agent="crypto")
    await store.initialize()
    store.bind_venue("paper")
    try:
        yield store
    finally:
        await store.close()


class Rig:
    """Two sleeves with their own engines over one paper book."""

    def __init__(self, storage: Storage, cfg: SleevesSettings | None = None) -> None:
        self.storage = storage
        self.cfg = cfg or _cfg()
        self.executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        self.provider = FakeProvider()
        self.agent_engine = RiskEngine(AGENT_RISK)
        self.engines = {
            spec.name: RiskEngine(sleeve_risk_settings(AGENT_RISK, AGENT_RISK, spec.risk_overrides))
            for spec in self.cfg.strategies
        }
        self.book = SleeveBook(
            self.cfg, storage, engines=self.engines, backstop_engine=self.agent_engine
        )

    async def start(self) -> Rig:
        await self.book.ensure_allocation(await self.portfolio())
        return self

    async def portfolio(self) -> PortfolioState:
        return PortfolioState(
            cash=await self.executor.get_cash(), positions=await self.executor.get_positions()
        )

    def pipeline(self, name: str, signal: TradeSignal) -> DecisionPipeline:
        return DecisionPipeline(
            provider=self.provider,
            llm_client=_llm(signal),
            risk_engine=self.engines[name],
            executor=self.executor,
            system_prompt=system_prompt_for(self.book.spec(name).playbook),
            storage=self.storage,
            strategy=name,
            sleeve_book=self.book,
        )


def _signal(action: Action = Action.BUY, symbol: str = "BTC/EUR", stop: float = 95.0):
    return TradeSignal(
        symbol=symbol,
        action=action,
        confidence=0.8,
        reasoning="setup",
        stop_loss=stop if action == Action.BUY else None,
        take_profit=110.0 if action == Action.BUY else None,
    )


async def _tagged_buy(rig: Rig, strategy: str, symbol: str, qty: float, price: float = 100.0):
    decision_id = await rig.storage.save_llm_decision(
        symbol=symbol,
        action="buy",
        confidence=0.8,
        reasoning="r",
        stop_loss=None,
        take_profit=None,
        risk_verdict="approved",
        risk_reason=None,
        strategy=strategy,
    )
    order = await rig.executor.place_order(
        symbol=symbol, side=OrderSide.BUY, quantity=qty, price=price, decision_id=decision_id
    )
    assert order.status == "filled"


# ── Limits ────────────────────────────────────────────────────


class TestSleeveRiskSettings:
    def test_sleeve_overrides_layer_over_the_agent_block(self) -> None:
        settings = sleeve_risk_settings(AGENT_RISK, AGENT_RISK, {"max_drawdown_pct": 0.15})
        assert settings.max_drawdown_pct == 0.15  # looser than the agent — by design (§4.8)
        assert settings.max_position_pct == AGENT_RISK.max_position_pct

    def test_agent_wide_tightening_caps_every_sleeve(self) -> None:
        live = RiskSettings(max_position_pct=0.1, max_drawdown_pct=0.04, min_confidence=0.7)
        settings = sleeve_risk_settings(
            AGENT_RISK, live, {"max_drawdown_pct": 0.15, "min_confidence": 0.5}
        )
        assert settings.max_drawdown_pct == 0.04
        assert settings.min_confidence == 0.7
        # A sleeve already tighter than the operator's cap keeps its own value.
        assert (
            sleeve_risk_settings(AGENT_RISK, live, {"max_drawdown_pct": 0.02}).max_drawdown_pct
            == 0.02
        )

    async def test_apply_risk_updates_engines_in_place(self, storage: Storage) -> None:
        rig = Rig(storage)
        swing_settings = rig.engines["swing"].settings
        live = RiskSettings(max_position_pct=0.05, max_drawdown_pct=0.05, max_open_positions=5)
        rig.book.apply_risk(AGENT_RISK, live)
        assert rig.engines["swing"].settings is swing_settings  # same object the pipeline reads
        assert swing_settings.max_position_pct == 0.05
        rig.book.apply_risk(AGENT_RISK, AGENT_RISK)  # override removed → back to the sleeve's own
        assert swing_settings.max_position_pct == 0.1


# ── Allocation & equity ───────────────────────────────────────


class TestAllocationAndEquity:
    async def test_allocation_is_cost_basis_and_recorded_once(self, storage: Storage) -> None:
        rig = Rig(storage)
        await _tagged_buy(rig, "swing", "BTC/EUR", 1.0)
        rig.executor.update_price("BTC/EUR", 150.0)  # an open gain is not capital
        first = await rig.book.ensure_allocation(await rig.portfolio())
        assert first.base_equity == pytest.approx(100_000.0)
        assert first.weights == {"swing": 0.5, "position": 0.5}
        again = await rig.book.ensure_allocation(await rig.portfolio())
        assert again.created_at == first.created_at  # same weights → same row

        reweighted = Rig(storage, _cfg(weights=(0.7, 0.3)))
        reweighted.executor = rig.executor
        changed = await reweighted.book.ensure_allocation(await rig.portfolio())
        assert changed.weights == {"swing": 0.7, "position": 0.3}
        assert (await storage.get_latest_allocation()).reason == "weights_changed"

    async def test_sleeve_equity_is_capital_plus_own_pnl(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        await _tagged_buy(rig, "swing", "BTC/EUR", 1.0)
        rig.executor.update_price("BTC/EUR", 110.0)
        await storage.save_order(
            "c1", "ETH/EUR", "sell", 1, 100, "filled", filled_at=datetime.now(UTC),
            realized_pnl=-20.0, strategy="swing",
        )  # fmt: skip
        portfolio = await rig.portfolio()
        swing = await rig.book.sleeve_equity("swing", portfolio, rig.executor)
        position = await rig.book.sleeve_equity("position", portfolio, rig.executor)
        assert swing.equity == pytest.approx(50_000 - 20 + 10)
        assert swing.portfolio.cash == pytest.approx(swing.equity - 110.0)
        assert [p.symbol for p in swing.portfolio.positions] == ["BTC/EUR"]
        assert position.equity == pytest.approx(50_000) and position.portfolio.positions == []
        # The sleeves add up to the agent's equity (weights sum to 1).
        assert swing.equity + position.equity == pytest.approx(portfolio.total_value - 20.0)

    async def test_equity_needs_an_allocation(self, storage: Storage) -> None:
        rig = Rig(storage)
        with pytest.raises(RuntimeError, match="allocation"):
            await rig.book.sleeve_equity("swing", await rig.portfolio(), rig.executor)


# ── Gates ─────────────────────────────────────────────────────


class TestPerSleeveGates:
    async def test_sizes_on_the_sleeve_book(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        result = await rig.pipeline("swing", _signal()).run("BTC/EUR", "1h")
        assert result.executed
        # 10 % of the sleeve's 50k — not of the agent's 100k.
        assert result.order_result.quantity == pytest.approx(50.0)

    async def test_prompt_shows_the_sleeve_book(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        pipe = rig.pipeline("position", _signal(Action.HOLD))
        await pipe.run("BTC/EUR", "4h")
        prompt = pipe.llm_client.ask_trade_signal.await_args.kwargs["user_prompt"]
        assert "Cash: 50000.00 of total equity 50000.00" in prompt

    async def test_max_open_positions_counts_only_own(self, storage: Storage) -> None:
        rig = await Rig(storage, _cfg(swing_risk={"max_open_positions": 1})).start()
        await _tagged_buy(rig, "position", "ETH/EUR", 1.0)
        assert (await rig.pipeline("swing", _signal()).run("BTC/EUR", "1h")).executed
        blocked = await rig.pipeline("swing", _signal(symbol="SOL/EUR")).run("SOL/EUR", "1h")
        assert blocked.risk_result.verdict.value == "rejected"
        assert "Max open positions (1)" in blocked.risk_result.reason

    async def test_sleeve_drawdown_blocks_only_that_sleeve(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        rig.engines["swing"].seed_peak_equity(60_000)  # swing at 50k: -16.7 % > 6 %
        swing = await rig.pipeline("swing", _signal()).run("BTC/EUR", "1h")
        assert "Drawdown" in (swing.risk_result.reason or "")
        position = await rig.pipeline("position", _signal()).run("BTC/EUR", "4h")
        assert position.executed

    async def test_backstop_blocks_entries_never_exits(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        await _tagged_buy(rig, "swing", "BTC/EUR", 1.0)
        rig.agent_engine.seed_peak_equity(200_000)  # agent at 100k: -50 % > 20 %
        entry = await rig.pipeline("position", _signal(symbol="ETH/EUR")).run("ETH/EUR", "4h")
        assert entry.risk_result.verdict.value == "rejected"
        assert "backstop" in entry.risk_result.reason
        exit_ = await rig.pipeline("swing", _signal(Action.SELL)).run("BTC/EUR", "1h")
        assert exit_.executed and exit_.order_result.side == OrderSide.SELL

    async def test_buy_never_spends_past_agent_cash(self, storage: Storage) -> None:
        cfg = _cfg(swing_risk={"max_position_pct": 0.5})
        rig = await Rig(storage, cfg).start()
        await _tagged_buy(rig, "position", "ETH/EUR", 900.0)  # agent cash → 10k
        result = await rig.pipeline("swing", _signal()).run("BTC/EUR", "1h")
        assert result.executed
        # Sleeve cash says 25k is fine, the venue only has 10k.
        assert result.order_result.quantity == pytest.approx(100.0)

    async def test_exit_outcome_goes_to_the_owning_sleeve(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        assert (await rig.pipeline("swing", _signal()).run("BTC/EUR", "1h")).executed
        rig.provider.last_close = 90.0  # through the 95 stop
        result = await rig.pipeline("position", _signal(Action.HOLD)).run("BTC/EUR", "4h")
        assert result.auto_exit and result.strategy == "swing"
        assert rig.engines["swing"]._loss_tracker.consecutive_losses == 1
        assert rig.engines["position"]._loss_tracker.consecutive_losses == 0


# ── Snapshots, rehydration, agent ─────────────────────────────


class TestSnapshotsAndRestore:
    async def test_snapshots_seed_trackers_after_restart(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        await _tagged_buy(rig, "swing", "BTC/EUR", 10.0)
        rig.executor.update_price("BTC/EUR", 120.0)
        books = {b.strategy: b for b in await rig.book.record_snapshots(rig.executor)}
        assert books["swing"].equity == pytest.approx(50_200.0)
        rows = await storage.get_latest_sleeve_snapshots()
        assert [(r.strategy, r.open_positions) for r in rows] == [("position", 0), ("swing", 1)]
        await storage.save_order(
            "c1", "BTC/EUR", "sell", 1, 90, "filled", filled_at=datetime.now(UTC),
            realized_pnl=-10.0, strategy="swing",
        )  # fmt: skip

        restarted = await Rig(storage).start()
        await restarted.book.restore()
        swing = restarted.engines["swing"]
        assert swing.peak_equity == pytest.approx(50_200.0)
        assert swing._daily_tracker.start_of_day_value == pytest.approx(50_200.0)
        assert swing._loss_tracker.consecutive_losses == 1
        assert restarted.engines["position"]._loss_tracker.consecutive_losses == 0

    async def test_no_snapshots_without_allocation(self, storage: Storage) -> None:
        rig = Rig(storage)
        assert await rig.book.record_snapshots(rig.executor) == []

    async def test_agent_cycle_writes_one_snapshot_per_sleeve(self, storage: Storage) -> None:
        rig = await Rig(storage).start()
        swing, position = rig.pipeline("swing", _signal()), rig.pipeline("position", _signal())
        agent = CryptoAgent(
            pipeline=swing,
            storage=storage,
            risk_engine=rig.agent_engine,
            llm_client=swing.llm_client,
            pairs=["BTC/EUR"],
        )
        agent.set_sleeves(
            [SleeveRun("swing", swing, "1h"), SleeveRun("position", position, "4h")], rig.book
        )
        await agent.run_cycle()
        rows = await storage.get_latest_sleeve_snapshots()
        assert {r.strategy for r in rows} == {"swing", "position"}
        swing_row = next(r for r in rows if r.strategy == "swing")
        assert swing_row.open_positions == 1


class _ReconcilingExecutor(PaperExecutor):
    """Paper book plus a venue-style reconciliation hook with one late closing fill."""

    def __init__(self, decision_id: int) -> None:
        super().__init__(initial_cash=100_000, slippage_pct=0.0)
        self._decision_id = decision_id
        self.confirmed: list[str] = []

    async def reconcile_open_orders(self):
        from src.core.models import OrderResult

        return [
            OrderResult(
                order_id="venue-1",
                symbol="BTC/EUR",
                side=OrderSide.SELL,
                quantity=1.0,
                price=90.0,
                status="filled",
                filled_at=datetime.now(UTC),
                realized_pnl=-10.0,
            )
        ]

    def pending_decision_id(self, order_id: str) -> int | None:
        return self._decision_id

    def confirm_reconciled(self, order_id: str) -> None:
        self.confirmed.append(order_id)


class TestReconciledFills:
    async def test_late_fill_goes_to_the_deciding_sleeve(self, storage: Storage) -> None:
        rig = Rig(storage)
        decision_id = await storage.save_llm_decision(
            symbol="BTC/EUR", action="sell", confidence=0.8, reasoning="r", stop_loss=None,
            take_profit=None, risk_verdict="approved", risk_reason=None, strategy="position",
        )  # fmt: skip
        rig.executor = _ReconcilingExecutor(decision_id)
        await rig.start()
        pipe = rig.pipeline("position", _signal(Action.HOLD))
        agent = CryptoAgent(
            pipeline=pipe,
            storage=storage,
            risk_engine=rig.agent_engine,
            llm_client=pipe.llm_client,
            pairs=[],
        )
        agent.set_sleeves([SleeveRun("position", pipe, "4h")], rig.book)
        await agent._reconcile_orders()
        assert rig.engines["position"]._loss_tracker.consecutive_losses == 1
        assert rig.engines["swing"]._loss_tracker.consecutive_losses == 0
        assert rig.agent_engine._loss_tracker.consecutive_losses == 0
        # The lost order row is re-created with its sleeve tag.
        (row,) = await storage.get_recent_orders()
        assert row.order_id == "venue-1" and row.strategy == "position"
        assert rig.executor.confirmed == ["venue-1"]
