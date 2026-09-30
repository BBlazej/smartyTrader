"""Strategy sleeves (§7.71): config, strategy tagging, symbol lock, time stops.

Sleeves are extra decision pipelines inside one agent. These tests pin the
contract: every decision/order carries its sleeve, a held symbol is locked to the
sleeve that owns it (derived from the ledger's entry decisions — restart-safe),
the owning sleeve's time stop closes stale positions without an LLM call, and
each sleeve learns from and times its bars on only its own history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.crypto_agent import CryptoAgent
from src.analysis.prompt_builder import DEFAULT_SYSTEM_PROMPT, PLAYBOOKS, system_prompt_for
from src.core.config import SLEEVE_PLAYBOOKS, RiskSettings, Settings, SleeveSpec, SleevesSettings
from src.core.decision_pipeline import DecisionPipeline
from src.core.models import OHLCV, Action, MarketSnapshot, OrderSide, TradeSignal
from src.core.risk_engine import RiskEngine
from src.core.runner import run_agent
from src.core.sleeves import TIME_STOP, SleeveBook, SleeveRun
from src.core.storage import Storage
from src.execution.paper_executor import PaperExecutor

SWING = {"timeframe": "1h", "playbook": "swing", "holding": {"max_hours": 72}}
POSITION = {"timeframe": "4h", "playbook": "position", "holding": {"max_days": 28}}


def _sleeves(**strategies: dict) -> SleevesSettings:
    return SleevesSettings(enabled=True, strategies=strategies or {"swing": SWING})


# ── Config ────────────────────────────────────────────────────


class TestSleeveConfig:
    def test_equal_weights_and_holding_units(self) -> None:
        cfg = _sleeves(crypto_swing=SWING, crypto_position=POSITION)
        assert [s.name for s in cfg.strategies] == ["crypto_swing", "crypto_position"]
        assert [s.weight for s in cfg.strategies] == [0.5, 0.5]
        assert cfg.get("crypto_swing").max_holding_hours == 72
        assert cfg.get("crypto_position").max_holding_hours == 28 * 24
        assert cfg.get("nope") is None

    def test_disabled_by_default(self) -> None:
        cfg = SleevesSettings()
        assert cfg.enabled is False and cfg.strategies == []

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"name": "Bad-Name", "timeframe": "1h"}, "lowercase"),
            ({"name": "x" * 21, "timeframe": "1h"}, "at most 20"),
            ({"name": "s", "timeframe": "hourly"}, "not a candle timeframe"),
            ({"name": "s", "timeframe": "1h", "playbook": "scalp"}, "playbook"),
            ({"name": "s", "timeframe": "1h", "holding": {"max_weeks": 2}}, "unknown keys"),
            (
                {"name": "s", "timeframe": "1h", "holding": {"max_hours": 1, "max_days": 1}},
                "not both",
            ),
            ({"name": "s", "timeframe": "1h", "holding": {"max_hours": 0}}, "> 0"),
            ({"name": "s", "timeframe": "1h", "weight": 1.5}, r"\(0, 1\]"),
            ({"name": "s", "timeframe": "1h", "risk": {"enforce_exit_levels": False}}, "risk"),
            ({"name": "s", "timeframe": "1h", "risk": {"max_leverage": 3}}, "risk"),
        ],
    )
    def test_spec_validation(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            SleeveSpec(**kwargs)

    def test_settings_validation(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            SleevesSettings(enabled=True)
        with pytest.raises(ValueError, match="sum to at most"):
            _sleeves(a={**SWING, "weight": 0.7}, b={**POSITION, "weight": 0.6})
        with pytest.raises(ValueError, match="every sleeve a weight"):
            _sleeves(a={**SWING, "weight": 0.5}, b=POSITION)
        with pytest.raises(ValueError, match="backstop"):
            SleevesSettings(backstop_max_drawdown_pct=1.0)

    def test_risk_overrides_accepted(self) -> None:
        spec = SleeveSpec(name="s", timeframe="4h", risk={"max_drawdown_pct": 0.15})
        assert spec.risk_overrides == {"max_drawdown_pct": 0.15}

    def test_playbook_texts_match_config(self) -> None:
        assert set(PLAYBOOKS) == set(SLEEVE_PLAYBOOKS)
        assert system_prompt_for(None) == DEFAULT_SYSTEM_PROMPT
        prompt = system_prompt_for("position")
        assert prompt.startswith(DEFAULT_SYSTEM_PROMPT) and "POSITION" in prompt

    def test_shipped_config_keeps_sleeves_off(self) -> None:
        settings = Settings()
        assert settings.crypto_agent.sleeves.enabled is False
        # The shipped (disabled) block still has to be a valid sleeve config.
        assert [s.name for s in settings.crypto_agent.sleeves.strategies] == [
            "crypto_swing",
            "crypto_position",
        ]


# ── Storage ───────────────────────────────────────────────────


@pytest.fixture()
async def storage(tmp_path: Path):
    store = Storage(str(tmp_path / "sleeves.db"), agent="crypto")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def _decision(storage: Storage, symbol: str = "BTC/EUR", strategy: str | None = None) -> int:
    return await storage.save_llm_decision(
        symbol=symbol,
        action="buy",
        confidence=0.8,
        reasoning="r",
        stop_loss=95.0,
        take_profit=110.0,
        risk_verdict="approved",
        risk_reason=None,
        **({"strategy": strategy} if strategy else {}),
    )


class TestStrategyStorage:
    async def test_decisions_tagged_and_filtered(self, storage: Storage) -> None:
        await _decision(storage, strategy="swing")
        await _decision(storage, strategy="position")
        await _decision(storage)
        swing = await storage.get_recent_decisions("BTC/EUR", strategy="swing")
        assert [row.strategy for row in swing] == ["swing"]
        assert len(await storage.get_recent_decisions("BTC/EUR")) == 3

    async def test_decision_strategy_lookup(self, storage: Storage) -> None:
        first = await _decision(storage, strategy="swing")
        second = await _decision(storage)
        meta = await storage.get_decision_strategies([first, second, 9999])
        assert meta[first][0] == "swing" and meta[first][1] is not None
        assert meta[second][0] is None
        assert 9999 not in meta

    async def test_orders_tagged_and_closing_fills_filtered(self, storage: Storage) -> None:
        now = datetime.now(UTC)
        await storage.save_order(
            "o1", "BTC/EUR", "sell", 1, 100, "filled", filled_at=now, realized_pnl=-5.0,
            strategy="swing",
        )  # fmt: skip
        await storage.save_order(
            "o2", "ETH/EUR", "sell", 1, 100, "filled", filled_at=now, realized_pnl=3.0,
            strategy="position",
        )  # fmt: skip
        swing = await storage.get_recent_closing_fills(strategy="swing")
        assert [o.order_id for o in swing] == ["o1"]
        assert len(await storage.get_recent_closing_fills()) == 2


# ── Ownership (SleeveBook) ────────────────────────────────────


async def _buy(executor: PaperExecutor, decision_id: int | None, symbol: str = "BTC/EUR") -> None:
    order = await executor.place_order(
        symbol=symbol, side=OrderSide.BUY, quantity=1.0, price=100.0, decision_id=decision_id
    )
    assert order.status == "filled"


class TestSleeveBook:
    async def test_owner_is_the_entry_decisions_sleeve(self, storage: Storage) -> None:
        book = SleeveBook(_sleeves(swing=SWING, position=POSITION), storage)
        executor = PaperExecutor(initial_cash=10_000, slippage_pct=0.0)
        await _buy(executor, await _decision(storage, strategy="position"))
        ownership = await book.ownership(executor, await executor.get_positions(), "BTC/EUR")
        assert ownership is not None and ownership.strategy == "position"
        assert ownership.opened_at is not None and ownership.opened_at.tzinfo is not None
        assert await book.owners(executor) == {"BTC/EUR": "position"}

    async def test_unknown_origin_belongs_to_the_first_sleeve(self, storage: Storage) -> None:
        book = SleeveBook(_sleeves(swing=SWING, position=POSITION), storage)
        executor = PaperExecutor(initial_cash=10_000, slippage_pct=0.0)
        await _buy(executor, None)  # synthetic / manual lot
        await _buy(executor, await _decision(storage), symbol="ETH/EUR")  # pre-§7.71 row
        positions = await executor.get_positions()
        for symbol in ("BTC/EUR", "ETH/EUR"):
            ownership = await book.ownership(executor, positions, symbol)
            assert ownership is not None and ownership.strategy == "swing"
        # No known decision time → no time stop is guessed.
        btc = await book.ownership(executor, positions, "BTC/EUR")
        assert btc.opened_at is None
        assert book.time_stop_due(btc, datetime.now(UTC) + timedelta(days=365)) is False

    async def test_flat_symbol_has_no_owner(self, storage: Storage) -> None:
        book = SleeveBook(_sleeves(), storage)
        executor = PaperExecutor(initial_cash=10_000)
        assert await book.ownership(executor, [], "BTC/EUR") is None

    async def test_time_stop_and_held_hours(self, storage: Storage) -> None:
        book = SleeveBook(_sleeves(swing=SWING), storage)
        executor = PaperExecutor(initial_cash=10_000, slippage_pct=0.0)
        await _buy(executor, await _decision(storage, strategy="swing"))
        ownership = await book.ownership(executor, await executor.get_positions(), "BTC/EUR")
        assert ownership is not None and ownership.opened_at is not None
        start = ownership.opened_at
        assert book.time_stop_due(ownership, start + timedelta(hours=71)) is False
        assert book.time_stop_due(ownership, start + timedelta(hours=72)) is True
        assert book.held_hours(ownership, start + timedelta(hours=5)) == pytest.approx(5.0)

    async def test_decision_metadata_is_cached(self, storage: Storage) -> None:
        book = SleeveBook(_sleeves(), storage)
        executor = PaperExecutor(initial_cash=10_000, slippage_pct=0.0)
        await _buy(executor, await _decision(storage, strategy="swing"))
        positions = await executor.get_positions()
        with patch.object(
            storage, "get_decision_strategies", wraps=storage.get_decision_strategies
        ) as spy:
            await book.ownership(executor, positions, "BTC/EUR")
            await book.ownership(executor, positions, "BTC/EUR")
        assert spy.await_count == 1

    def test_requires_a_sleeve(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            SleeveBook(SleevesSettings(), MagicMock())


# ── Pipelines ─────────────────────────────────────────────────


class FakeProvider:
    """Flat-ish price series around 100; ``fetched_at`` moves the clock."""

    def __init__(self, fetched_at: datetime | None = None, last_close: float = 100.0) -> None:
        self.fetched_at = fetched_at
        self.last_close = last_close

    async def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
        candles = [
            OHLCV(open=100.0, high=101.0, low=99.0, close=100.0 + (i % 3) * 0.1, volume=10.0)
            for i in range(40)
        ]
        candles[-1].close = self.last_close
        snapshot = MarketSnapshot(symbol=symbol, timeframe=timeframe, candles=candles)
        if self.fetched_at is not None:
            snapshot.fetched_at = self.fetched_at
        return snapshot


def _signal(action: Action = Action.BUY) -> TradeSignal:
    return TradeSignal(
        symbol="BTC/EUR",
        action=action,
        confidence=0.8,
        reasoning="setup",
        stop_loss=95.0 if action == Action.BUY else None,
        take_profit=110.0 if action == Action.BUY else None,
    )


def _llm(signal: TradeSignal | None = None) -> MagicMock:
    llm = MagicMock()
    llm.ask_trade_signal = AsyncMock(side_effect=lambda **_: (signal or _signal()).model_copy())
    llm.last_metrics = None
    llm.close = AsyncMock()
    return llm


def _pipelines(
    storage: Storage,
    executor: PaperExecutor,
    provider: FakeProvider,
    llm: MagicMock,
    sleeves: SleevesSettings | None = None,
) -> tuple[SleeveBook, dict[str, DecisionPipeline]]:
    cfg = sleeves or _sleeves(swing=SWING, position=POSITION)
    book = SleeveBook(cfg, storage)
    risk = RiskEngine(RiskSettings(max_position_pct=0.1))
    pipelines = {
        spec.name: DecisionPipeline(
            provider=provider,
            llm_client=llm,
            risk_engine=risk,
            executor=executor,
            system_prompt=system_prompt_for(spec.playbook),
            storage=storage,
            strategy=spec.name,
            sleeve_book=book,
        )
        for spec in cfg.strategies
    }
    return book, pipelines


class TestSleevePipelines:
    async def test_entry_is_tagged_and_locks_the_symbol(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        llm = _llm()
        _, pipes = _pipelines(storage, executor, FakeProvider(), llm)

        entry = await pipes["swing"].run("BTC/EUR", "1h")
        assert entry.executed and entry.strategy == "swing"
        rows = await storage.get_recent_decisions("BTC/EUR")
        assert [row.strategy for row in rows] == ["swing"]

        locked = await pipes["position"].run("BTC/EUR", "4h")
        assert locked.skip_reason == "held by sleeve swing (symbol lock)"
        assert locked.strategy == "position" and locked.signal is None
        assert llm.ask_trade_signal.await_count == 1  # the locked sleeve was never asked

        again = await pipes["swing"].run("BTC/EUR", "1h")
        assert again.signal is not None  # the owner keeps deciding on its symbol

    async def test_system_prompt_and_book_carry_the_sleeve(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        llm = _llm(_signal(Action.HOLD))
        _, pipes = _pipelines(storage, executor, FakeProvider(), llm)
        await pipes["position"].run("BTC/EUR", "4h")
        kwargs = llm.ask_trade_signal.await_args.kwargs
        assert kwargs["system_prompt"] == system_prompt_for("position")
        assert (
            "Strategy sleeve: position — time stop: a position auto-closes after 28.0d"
            in (kwargs["user_prompt"])
        )

    async def test_time_stop_closes_without_the_llm(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        provider = FakeProvider()
        llm = _llm()
        _, pipes = _pipelines(storage, executor, provider, llm)
        assert (await pipes["swing"].run("BTC/EUR", "1h")).executed

        provider.fetched_at = datetime.now(UTC) + timedelta(hours=73)
        # Enforced from whichever sleeve's run sees it first — here the non-owner's.
        result = await pipes["position"].run("BTC/EUR", "4h")
        assert result.auto_exit and result.exit_reason == TIME_STOP
        assert result.strategy == "swing"  # the close belongs to the owning sleeve
        assert result.executed and result.order_result.side == OrderSide.SELL
        assert await executor.get_positions() == []
        assert llm.ask_trade_signal.await_count == 1

    async def test_time_stop_waits_for_the_limit(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        provider = FakeProvider()
        _, pipes = _pipelines(storage, executor, provider, _llm(_signal(Action.HOLD)))
        await _buy(executor, await _decision(storage, strategy="swing"))
        provider.fetched_at = datetime.now(UTC) + timedelta(hours=71)
        result = await pipes["swing"].run("BTC/EUR", "1h")
        assert not result.auto_exit and result.signal is not None

    async def test_exit_level_close_is_tagged_with_the_owner(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        provider = FakeProvider()
        _, pipes = _pipelines(storage, executor, provider, _llm())
        assert (await pipes["position"].run("BTC/EUR", "4h")).executed
        provider.last_close = 94.0  # through the 95 stop
        result = await pipes["swing"].run("BTC/EUR", "1h")
        assert result.auto_exit and result.exit_reason == "stop_loss"
        assert result.strategy == "position"

    async def test_bar_timing_and_history_are_per_sleeve(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        llm = _llm(_signal(Action.HOLD))
        book, pipes = _pipelines(storage, executor, FakeProvider(), llm)
        for pipe in pipes.values():
            pipe.decide_on_new_bar_only = True

        class Timed(FakeProvider):
            async def fetch_snapshot(self, symbol: str, timeframe: str) -> MarketSnapshot:
                snapshot = await super().fetch_snapshot(symbol, timeframe)
                start = datetime(2026, 9, 1, tzinfo=UTC)
                for i, candle in enumerate(snapshot.candles):
                    candle.timestamp = start + timedelta(hours=i)
                snapshot.fetched_at = start + timedelta(hours=40, minutes=5)
                return snapshot

        for pipe in pipes.values():
            pipe.provider = Timed()
        await pipes["swing"].run("BTC/EUR", "1h")
        again = await pipes["swing"].run("BTC/EUR", "1h")
        assert again.skip_reason is not None  # swing already decided this bar …
        other = await pipes["position"].run("BTC/EUR", "1h")
        assert other.skip_reason is None and other.signal is not None  # … position had not
        history = await pipes["position"].get_recent_decisions("BTC/EUR")
        assert len(history) == 1  # only its own row
        assert book.default == "swing"

    async def test_unreadable_book_blocks_entries_not_exits(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        llm = _llm()
        _, pipes = _pipelines(storage, executor, FakeProvider(), llm)
        with patch.object(
            storage, "get_decision_strategies", AsyncMock(side_effect=RuntimeError("db gone"))
        ):
            await _buy(executor, 1)
            result = await pipes["swing"].run("BTC/EUR", "1h")
        assert result.error is not None and "ownership" in result.error
        llm.ask_trade_signal.assert_not_awaited()


# ── Agent + runner wiring ─────────────────────────────────────


class TestAgentSleeves:
    async def test_cycle_runs_every_sleeve_and_tags_orders(self, storage: Storage) -> None:
        executor = PaperExecutor(initial_cash=100_000, slippage_pct=0.0)
        llm = _llm()
        book, pipes = _pipelines(storage, executor, FakeProvider(), llm)
        agent = CryptoAgent(
            pipeline=pipes["swing"],
            storage=storage,
            risk_engine=pipes["swing"].risk_engine,
            llm_client=llm,
            pairs=["BTC/EUR", "ETH/EUR"],
        )
        agent.set_sleeves(
            [
                SleeveRun("swing", pipes["swing"], "1h"),
                SleeveRun("position", pipes["position"], "4h"),
            ],
            book,
        )
        results = await agent.run_cycle()
        # 2 symbols × 2 sleeves; swing (listed first) wins both flat symbols.
        assert [(r.symbol, r.strategy) for r in results] == [
            ("BTC/EUR", "swing"),
            ("BTC/EUR", "position"),
            ("ETH/EUR", "swing"),
            ("ETH/EUR", "position"),
        ]
        assert [r.skip_reason is not None for r in results] == [False, True, False, True]
        orders = await storage.get_recent_orders()
        assert {o.strategy for o in orders} == {"swing"}

        await agent._close_all_positions()
        closes = [o for o in await storage.get_recent_orders() if o.side == "sell"]
        assert len(closes) == 2 and {o.strategy for o in closes} == {"swing"}


_SLEEVES_YAML = """
llm: {{endpoint: "http://localhost:1234/v1/chat/completions", model: m}}
crypto_agent:
  enabled: true
  interval_minutes: 5
  pairs: ["BTC/EUR"]
  decision_history_limit: 10
  sleeves:
    enabled: {enabled}
    strategies:
      crypto_swing: {{timeframe: "1h", playbook: swing, holding: {{max_hours: 72}}, risk: {{max_position_pct: 0.05}}}}
      crypto_position: {{timeframe: "4h", playbook: position}}
stocks_agent: {{enabled: false, interval_minutes: 60, symbols: ["AAPL"], decision_history_limit: 10}}
risk: {{max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}}
storage: {{database_path: "{db}"}}
monitoring: {{log_level: INFO}}
"""


class _Agent:
    def __init__(self, override: str | None = None) -> None:
        self.sleeves: list[SleeveRun] | None = None
        self.cycles = 0
        self._override = override
        self._applier = None

    def set_sleeves(self, runs: list[SleeveRun], book: SleeveBook) -> None:
        self.sleeves = runs

    def set_control_overrides_applier(self, applier) -> None:  # type: ignore[no-untyped-def]
        self._applier = applier

    def set_symbols(self, symbols: list[str]) -> None:
        return None

    async def run_cycle(self):
        if self._override is not None and self._applier is not None:
            self._applier(self._override)  # a dashboard save mid-run
        self.cycles += 1
        return []

    async def shutdown(self) -> None:
        return None


class TestRunnerSleeves:
    async def _run(self, tmp_path: Path, enabled: bool, override: str | None = None) -> _Agent:
        config = tmp_path / "settings.yaml"
        config.write_text(
            _SLEEVES_YAML.format(enabled=str(enabled).lower(), db=tmp_path / "runner.db")
        )
        settings = Settings(str(config))
        agent = _Agent(override)
        provider = MagicMock()
        provider.close = AsyncMock()
        executor = MagicMock()
        executor.close = AsyncMock()
        executor.get_cash = AsyncMock(return_value=10_000.0)
        executor.get_positions = AsyncMock(return_value=[])
        with (
            patch("src.core.runner.rehydrate_from_storage", new=AsyncMock()),
            patch("src.core.runner.prune_storage", new=AsyncMock()),
        ):
            await run_agent(
                settings,
                component="crypto",
                agent_enabled=True,
                interval_minutes=5,
                decision_history_limit=10,
                job_id="crypto_cycle",
                build_components=lambda: (provider, executor),
                build_agent=lambda pipeline, storage, risk_engine, llm_client: agent,
                run_once=True,
            )
        return agent

    async def test_enabled_sleeves_get_one_pipeline_each(self, tmp_path: Path) -> None:
        agent = await self._run(tmp_path, enabled=True)
        assert agent.sleeves is not None
        assert [(r.name, r.timeframe) for r in agent.sleeves] == [
            ("crypto_swing", "1h"),
            ("crypto_position", "4h"),
        ]
        swing, position = (r.pipeline for r in agent.sleeves)
        assert swing.strategy == "crypto_swing" and position.strategy == "crypto_position"
        assert position.system_prompt == system_prompt_for("position")
        assert swing.executor is position.executor
        assert agent.cycles == 1

    async def test_sleeves_get_own_engines_and_an_allocation(self, tmp_path: Path) -> None:
        agent = await self._run(tmp_path, enabled=True)
        swing, position = (r.pipeline.risk_engine for r in agent.sleeves)
        assert swing is not position
        assert swing.settings.max_position_pct == 0.05  # the sleeve's own override
        assert position.settings.max_position_pct == 0.1  # the agent block
        store = Storage(str(tmp_path / "paper_crypto.db"))  # the runner's book (§7.78)
        await store.initialize()
        allocation = await store.get_latest_allocation(agent="crypto")
        await store.close()
        assert allocation is not None and allocation.base_equity == 10_000.0

    async def test_agent_risk_tightening_caps_sleeves(self, tmp_path: Path) -> None:
        agent = await self._run(
            tmp_path, enabled=True, override='{"risk": {"max_position_pct": 0.03}}'
        )
        swing, position = (r.pipeline.risk_engine for r in agent.sleeves)
        assert swing.settings.max_position_pct == 0.03
        assert position.settings.max_position_pct == 0.03

    async def test_disabled_sleeves_touch_nothing(self, tmp_path: Path) -> None:
        agent = await self._run(tmp_path, enabled=False)
        assert agent.sleeves is None and agent.cycles == 1


class TestPendingBuyLock:
    """§7.72: a working venue BUY claims its symbol before it fills."""

    async def test_pending_buy_locks_the_symbol_for_other_sleeves(self, storage: Storage) -> None:
        book = SleeveBook(_sleeves(swing=SWING, position=POSITION), storage)
        decision_id = await _decision(storage, strategy="position")
        executor = MagicMock()
        executor.pending_entry_decision_ids = MagicMock(return_value=[decision_id])
        ownership = await book.ownership(executor, [], "BTC/EUR")
        assert ownership is not None and ownership.strategy == "position"
        assert ownership.opened_at is None  # no fill yet → no time-stop clock
        executor.pending_entry_decision_ids.return_value = []
        assert await book.ownership(executor, [], "BTC/EUR") is None

    async def test_ccxt_executor_reports_working_buys(self) -> None:
        from src.execution.ccxt_executor import CcxtExecutor, PendingOrderRecord

        ex = CcxtExecutor(MagicMock(), quote_currency="EUR", venue="myokx-sandbox")
        ex.load_pending_orders(
            [
                PendingOrderRecord("1", "BTC/EUR", OrderSide.BUY, 1.0, decision_id=5),
                PendingOrderRecord("2", "BTC/EUR", OrderSide.SELL, 1.0, decision_id=6),
                PendingOrderRecord("3", "ETH/EUR", OrderSide.BUY, 1.0, decision_id=7),
            ]
        )
        assert ex.pending_entry_decision_ids("BTC/EUR") == [5]
