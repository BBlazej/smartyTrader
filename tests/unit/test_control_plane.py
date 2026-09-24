"""Control-plane tests (§7.15 P1): storage rows, whitelist, application, agent checks."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.agents.base_agent import BaseTradingAgent
from src.core.config import Settings
from src.core.control_config import SafeConfigOverrides, parse_and_apply, parse_overrides
from src.core.decision_pipeline import PipelineResult
from src.core.models import ClosedEntry, OrderResult, Position
from src.core.storage import Storage


@pytest.fixture()
async def storage(tmp_path):
    s = Storage(str(tmp_path / "control.db"))
    await s.initialize()
    yield s
    await s.close()


def _settings(tmp_path) -> Settings:
    config = tmp_path / "settings.yaml"
    config.write_text(
        """
llm: {endpoint: "http://localhost:1234/v1/chat/completions", model: m}
crypto_agent: {enabled: true, interval_minutes: 5, pairs: ["BTC/USDT"], decision_history_limit: 10}
stocks_agent: {enabled: false, interval_minutes: 60, symbols: ["AAPL"], market_hours: "08:00-22:00", decision_history_limit: 10}
risk: {max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}
storage: {database_path: "x.db"}
monitoring: {log_level: INFO}
"""
    )
    return Settings(str(config))


class TestAgentControlStorage:
    async def test_absent_row_returns_none(self, storage: Storage) -> None:
        assert await storage.get_agent_control("crypto") is None

    async def test_set_state_creates_row_and_round_trips(self, storage: Storage) -> None:
        row = await storage.set_agent_state("crypto", "paused")
        assert row.state == "paused"
        assert row.close_all_requested is False  # defaults kept on create

        fetched = await storage.get_agent_control("crypto")
        assert fetched is not None and fetched.state == "paused"

        await storage.set_agent_state("crypto", "running")
        assert (await storage.get_agent_control("crypto")).state == "running"

    async def test_invalid_state_rejected(self, storage: Storage) -> None:
        with pytest.raises(ValueError):
            await storage.set_agent_state("crypto", "annihilated")

    async def test_close_all_latch_round_trip(self, storage: Storage) -> None:
        row = await storage.request_close_all("stocks")
        assert row.close_all_requested is True
        row = await storage.request_close_all("stocks", requested=False)
        assert row.close_all_requested is False

    async def test_health_stamps_time_and_error(self, storage: Storage) -> None:
        before = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        row = await storage.record_cycle_health("crypto", last_error="llm down")
        assert row.last_cycle_at is not None and row.last_cycle_at >= before
        assert row.last_error == "llm down"

        row = await storage.record_cycle_health("crypto", last_error=None)
        assert row.last_error is None  # a clean cycle clears the stale error

    async def test_config_override_round_trip(self, storage: Storage) -> None:
        await storage.set_config_override("crypto", '{"min_confidence": 0.7}')
        assert (await storage.get_agent_control("crypto")).config_override_json == (
            '{"min_confidence": 0.7}'
        )
        await storage.set_config_override("crypto", None)
        assert (await storage.get_agent_control("crypto")).config_override_json is None


class TestConfigWhitelist:
    def test_valid_overrides_parse(self) -> None:
        o = SafeConfigOverrides.model_validate(
            {
                "interval_minutes": 15,
                "risk": {"max_position_pct": 0.05},
                "execution": {"paper_fee_pct": 0.003},
            }
        )
        assert o.risk is not None and o.risk.max_position_pct == 0.05

    @pytest.mark.parametrize(
        "payload",
        [
            {"api_key": "super-secret"},  # credential-shaped → rejected wholesale
            {"llm": {"endpoint": "http://evil"}},  # llm.* not on the whitelist
            {"interval_minutes": 0},  # below range
            {"risk": {"max_position_pct": 5.0}},  # above range
            {"storage": {"database_path": "/tmp/evil.db"}},  # storage not on the whitelist
        ],
    )
    def test_non_whitelisted_or_invalid_rejected(self, payload: dict) -> None:
        with pytest.raises(ValidationError):
            SafeConfigOverrides.model_validate(payload)

    def test_parse_overrides_empty_is_none(self) -> None:
        assert parse_overrides(None) is None
        assert parse_overrides("") is None


class TestApplyOverrides:
    def test_mutates_live_objects_and_reports_changes(self, tmp_path) -> None:
        settings = _settings(tmp_path)

        class _Pipeline:
            executor = SimpleNamespace(fee_pct=0.0026, slippage_pct=0.001)
            decision_history_limit = 10

        pipeline = _Pipeline()
        agent = SimpleNamespace(set_symbols=lambda s: setattr(agent, "symbols", s))

        changed = parse_and_apply(
            settings,
            "crypto",
            '{"interval_minutes": 30, "risk": {"min_confidence": 0.75}, '
            '"execution": {"paper_fee_pct": 0.005}, "decision_history_limit": 3}',
            pipeline=pipeline,
            agent=agent,
        )

        assert settings.crypto_agent.interval_minutes == 30
        assert settings.risk.min_confidence == 0.75  # RiskEngine holds this same object
        assert pipeline.executor.fee_pct == 0.005
        assert pipeline.decision_history_limit == 3
        assert set(changed) >= {"interval_minutes", "min_confidence", "fee_pct"}

    def test_stocks_symbols_override_updates_agent_symbols(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        seen: list[list[str]] = []
        agent = SimpleNamespace(set_symbols=lambda s: seen.append(s))

        parse_and_apply(settings, "stocks", '{"symbols": ["MSFT", "NVDA"]}', agent=agent)

        assert settings.stocks_agent.symbols == ["MSFT", "NVDA"]
        assert seen == [["MSFT", "NVDA"]]

    def test_empty_override_changes_nothing(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        assert parse_and_apply(settings, "crypto", None) == []


def _agent_with(pipeline, stor):
    risk_engine = AsyncMock()
    # ``RiskEngine.update_daily_value`` is synchronous in the real class; an
    # auto-created AsyncMock attribute would return a coroutine that base_agent
    # (correctly) never awaits — pin the spec-mirroring sync mock instead (§7.29).
    risk_engine.update_daily_value = MagicMock()
    return BaseTradingAgent(
        pipeline=pipeline,
        storage=stor,
        risk_engine=risk_engine,
        llm_client=AsyncMock(),
        symbols=["BTC/USDT"],
        timeframe="1h",
        component="crypto",
    )


class TestAgentControlIntegration:
    async def test_paused_agent_skips_the_cycle(self) -> None:
        pipeline = AsyncMock()
        stor = AsyncMock()
        stor.get_agent_control.return_value = SimpleNamespace(
            state="paused", close_all_requested=False, config_override_json=None
        )
        agent = _agent_with(pipeline, stor)

        results = await agent.run_cycle()

        assert results == []
        pipeline.run.assert_not_called()
        pipeline.close_all_positions.assert_not_called()

    async def test_close_all_runs_then_latch_cleared_even_while_paused(self) -> None:
        order = OrderResult(
            order_id="o1",
            symbol="BTC/USDT",
            side="sell",
            quantity=1.0,
            price=100.0,
            status="filled",
            realized_pnl=-5.0,
            closed_entries=[ClosedEntry(entry_decision_id=7, pnl=-5.0)],
        )
        pipeline = AsyncMock()
        pipeline.close_all_positions.return_value = [("BTC/USDT", order)]
        stor = AsyncMock()
        stor.get_agent_control.return_value = SimpleNamespace(
            state="paused", close_all_requested=True, config_override_json=None
        )
        agent = _agent_with(pipeline, stor)

        results = await agent.run_cycle()

        assert results == []  # paused afterwards — no decision cycles
        pipeline.close_all_positions.assert_awaited_once()
        stor.request_close_all.assert_awaited_once_with("crypto", requested=False)
        # the close was persisted + its outcome backfilled to the entry decision
        stor.save_order.assert_awaited_once()
        stor.add_realized_pnl.assert_awaited_once_with(7, -5.0)

    async def test_override_applier_receives_raw_json(self) -> None:
        pipeline = AsyncMock()
        pipeline.run.return_value = PipelineResult(symbol="BTC/USDT")
        stor = AsyncMock()
        stor.get_agent_control.return_value = SimpleNamespace(
            state="running", close_all_requested=False, config_override_json="{bad json"
        )
        applied: list[str] = []
        agent = _agent_with(pipeline, stor)
        agent.set_control_overrides_applier(applied.append)

        await agent.run_cycle()

        assert applied == ["{bad json"]  # applier decides; the cycle proceeds regardless

    async def test_override_applier_failure_does_not_kill_cycle(self) -> None:
        pipeline = AsyncMock()
        pipeline.run.return_value = PipelineResult(symbol="BTC/USDT")
        stor = AsyncMock()
        stor.get_agent_control.return_value = SimpleNamespace(
            state="running", close_all_requested=False, config_override_json="{}"
        )
        agent = _agent_with(pipeline, stor)
        agent.set_control_overrides_applier(lambda raw: (_ for _ in ()).throw(ValueError("nope")))

        results = await agent.run_cycle()  # must not raise

        assert len(results) == 1

    async def test_control_read_failure_runs_with_defaults(self) -> None:
        pipeline = AsyncMock()
        pipeline.run.return_value = PipelineResult(symbol="BTC/USDT")
        stor = AsyncMock()
        stor.get_agent_control.side_effect = RuntimeError("db down")
        agent = _agent_with(pipeline, stor)

        results = await agent.run_cycle()

        assert len(results) == 1
        pipeline.run.assert_awaited_once()

    async def test_unconfigured_mock_storage_never_triggers_actions(self) -> None:
        # Regression: AsyncMock's auto-created attributes must not look like a
        # close-all latch or a pause to the strict control checks.
        pipeline = AsyncMock()
        pipeline.run.return_value = PipelineResult(symbol="BTC/USDT")
        agent = _agent_with(pipeline, AsyncMock())

        results = await agent.run_cycle()

        assert len(results) == 1
        pipeline.close_all_positions.assert_not_called()


class TestCloseAllPositions:
    async def test_closes_every_position_at_its_mark(self) -> None:
        from src.core.decision_pipeline import DecisionPipeline
        from src.execution.paper_executor import PaperExecutor

        executor = PaperExecutor(initial_cash=10_000.0, slippage_pct=0.0)
        await executor.place_order("BTC/USDT", "buy", 1.0, price=100.0)
        await executor.place_order("ETH/USDT", "buy", 10.0, price=50.0)
        executor.update_price("BTC/USDT", 120.0)
        executor.update_price("ETH/USDT", 45.0)

        pipeline = DecisionPipeline(
            provider=AsyncMock(),
            llm_client=AsyncMock(),
            risk_engine=AsyncMock(),
            executor=executor,
        )

        closed = await pipeline.close_all_positions()

        assert {symbol for symbol, _ in closed} == {"BTC/USDT", "ETH/USDT"}
        pnls = {symbol: order.realized_pnl for symbol, order in closed}
        assert pnls["BTC/USDT"] == pytest.approx(20.0)
        assert pnls["ETH/USDT"] == pytest.approx(-50.0)
        assert await executor.get_positions() == []

    async def test_position_without_a_mark_is_skipped(self) -> None:
        from src.core.decision_pipeline import DecisionPipeline

        class _NoMarkExecutor:
            async def get_positions(self):
                return [Position(symbol="X", quantity=1.0, avg_entry_price=10.0, current_price=0.0)]

            async def place_order(self, **kwargs):  # must not be called
                raise AssertionError("must not place an order without a price")

        pipeline = DecisionPipeline(
            provider=AsyncMock(),
            llm_client=AsyncMock(),
            risk_engine=AsyncMock(),
            executor=_NoMarkExecutor(),
        )
        assert await pipeline.close_all_positions() == []


class TestTightenOnlyOverrides:
    """§7.43: risk overrides may only tighten the YAML limits."""

    @staticmethod
    def _baseline():
        from src.core.config import RiskSettings

        return RiskSettings(
            max_position_pct=0.10,
            daily_loss_limit_pct=0.02,
            max_drawdown_pct=0.05,
            consecutive_losses_cooldown_minutes=60,
            max_open_positions=5,
            min_confidence=0.6,
        )

    @pytest.mark.parametrize(
        "risk",
        [
            {"max_position_pct": 0.2},
            {"daily_loss_limit_pct": 0.5},
            {"max_drawdown_pct": 1.0},
            {"max_open_positions": 10},
            {"min_confidence": 0.1},
            {"consecutive_losses_cooldown_minutes": 0},
        ],
    )
    def test_each_loosening_is_rejected(self, risk: dict) -> None:
        from src.core.control_config import validate_overrides_payload

        model, error = validate_overrides_payload({"risk": risk}, baseline=self._baseline())
        assert model is None and "may only tighten" in (error or "")

    def test_tightening_and_equal_values_pass(self) -> None:
        from src.core.control_config import validate_overrides_payload

        payload = {
            "risk": {
                "max_position_pct": 0.05,
                "max_drawdown_pct": 0.05,  # equal is not looser
                "min_confidence": 0.8,
                "consecutive_losses_cooldown_minutes": 120,
            }
        }
        model, error = validate_overrides_payload(payload, baseline=self._baseline())
        assert error is None and model is not None

    def test_enforce_exit_levels_is_off_the_surface(self) -> None:
        from src.core.control_config import validate_overrides_payload

        model, error = validate_overrides_payload({"risk": {"enforce_exit_levels": False}})
        assert model is None and "enforce_exit_levels" in (error or "")

    def test_legacy_stored_row_keeps_its_other_overrides(self) -> None:
        from src.core.control_config import parse_overrides

        legacy = '{"risk": {"min_confidence": 0.7, "enforce_exit_levels": false}}'
        parsed = parse_overrides(legacy)
        assert parsed is not None and parsed.risk.min_confidence == 0.7

    def test_apply_skips_stored_values_looser_than_yaml(self) -> None:
        """The YAML may be tightened after an override was stored: never loosen live."""
        from types import SimpleNamespace

        from src.core.control_config import parse_and_apply

        live = self._baseline()
        live.max_drawdown_pct = 0.03  # YAML now says 3%
        settings = SimpleNamespace(
            risk=live,
            risk_baseline=SimpleNamespace(**{**vars(self._baseline()), "max_drawdown_pct": 0.03}),
            crypto_agent=SimpleNamespace(),
        )
        changed = parse_and_apply(
            settings, "crypto", '{"risk": {"max_drawdown_pct": 0.05, "min_confidence": 0.7}}'
        )
        assert live.max_drawdown_pct == 0.03  # stored 5% would loosen → skipped
        assert live.min_confidence == 0.7
        assert "min_confidence" in changed and "max_drawdown_pct" not in changed


def test_shipped_config_keeps_process_launch_off() -> None:
    from src.core.config import Settings

    assert Settings().dashboard.allow_launch is False


def test_allowed_hosts_helper() -> None:
    from src.core.web_security import allowed_hosts

    assert allowed_hosts("0.0.0.0") == ["127.0.0.1", "localhost", "::1", "[::1]"]
    assert allowed_hosts("192.168.1.5", ["dash.lan"])[-2:] == ["192.168.1.5", "dash.lan"]
