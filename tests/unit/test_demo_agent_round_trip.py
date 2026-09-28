"""Unit tests for the agent-driven OKX demo round trip (§7.28) — the offline parts."""

from __future__ import annotations

import pytest

from scripts.demo_agent_round_trip import (
    ScriptedLLM,
    _demo_components,
    high_water_ids,
    read_back,
    smoke_settings,
    smoke_signal,
)
from src.core.config import RiskSettings, Settings
from src.core.models import Action
from src.core.risk_engine import RiskEngine
from src.core.storage import Storage


class TestScriptedSignal:
    def test_buy_carries_a_stop_and_target_the_gate_accepts(self) -> None:
        signal = smoke_signal(Action.BUY, "BTC/EUR", 73_000.0)
        assert signal.stop_loss == pytest.approx(69_350.0)
        assert signal.take_profit == pytest.approx(80_300.0)
        assert "SMOKE TEST" in signal.reasoning
        # §7.54 geometry: stop below price, within max_stop_distance_pct.
        engine = RiskEngine(RiskSettings())
        from src.core.models import PortfolioState

        verdict = engine.evaluate(
            signal, PortfolioState(cash=4_600.0), planned_notional=460.0, current_price=73_000.0
        )
        assert verdict.verdict.value == "approved", verdict.reason

    def test_sell_has_no_levels(self) -> None:
        signal = smoke_signal(Action.SELL, "BTC/EUR", 73_000.0)
        assert signal.stop_loss is None and signal.take_profit is None

    async def test_scripted_llm_returns_a_fresh_copy_each_call(self) -> None:
        llm = ScriptedLLM(smoke_signal(Action.BUY, "BTC/EUR", 73_000.0))
        first = await llm.ask_trade_signal("s", "u")
        first.symbol = "MUTATED"
        second = await llm.ask_trade_signal("s", "u")
        assert second.symbol == "BTC/EUR"
        assert llm.calls == 2
        assert llm.last_metrics is None
        await llm.close()


class TestSmokeSettings:
    def test_one_symbol_and_no_watchlist(self) -> None:
        settings = smoke_settings(Settings(), "ETH/EUR")
        assert settings.crypto_agent.pairs == ["ETH/EUR"]
        assert settings.crypto_agent.watchlist.enabled is False

    def test_refuses_a_paper_executor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EXCHANGE_API_KEY", raising=False)
        with pytest.raises(SystemExit, match="refusing"):
            _demo_components(Settings())


class TestReadBack:
    async def test_reports_only_rows_written_after_the_high_water_mark(
        self, tmp_db_path: str
    ) -> None:
        storage = Storage(tmp_db_path, agent="crypto")
        await storage.initialize()
        storage.bind_venue("myokx-sandbox")
        await storage.save_order("OLD", "BTC/EUR", "buy", 1.0, 1.0, "filled")
        since = high_water_ids(tmp_db_path)
        await storage.save_order("NEW", "BTC/EUR", "buy", 1.0, 1.0, "filled")
        await storage.save_portfolio_snapshot(cash=1.0, positions_json="[]", total_value=1.0)
        await storage.close()

        report = read_back(tmp_db_path, since)
        assert [o["order_id"] for o in report["orders"]] == ["NEW"]
        assert report["orders"][0]["venue"] == "myokx-sandbox"
        assert len(report["portfolio_snapshots"]) == 1
        assert report["decisions"] == []
