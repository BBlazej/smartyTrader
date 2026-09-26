"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import Settings


@pytest.fixture()
def config_path() -> str:
    return str(Path(__file__).parents[2] / "config" / "settings.yaml")


class TestSettingsLoad:
    def test_loads_defaults(self, config_path: str) -> None:
        s = Settings(config_path=config_path)

        assert s.llm.endpoint == "http://127.0.0.1:1234/v1/chat/completions"
        assert s.llm.model == "qwen/qwen3.8-27b"
        assert s.risk.max_position_pct == 0.10
        assert s.risk.min_confidence == 0.6
        # §7.33 knobs ship in settings.yaml and load cleanly.
        assert s.llm.seed is None
        assert s.llm.max_response_chars == 36_000
        # The size guard must leave room for a full max_tokens completion (~4 chars
        # per token), or long answers are discarded before the parser.
        assert s.llm.max_response_chars >= 4 * s.llm.max_tokens

    def test_crypto_agent_config(self, config_path: str) -> None:
        s = Settings(config_path=config_path)

        assert s.crypto_agent.enabled is True
        # §7.64: OKX Europe, EUR-quoted pairs (USDT isn't tradable for EEA accounts).
        assert s.crypto_agent.exchange == "myokx"
        assert s.crypto_agent.quote_currency == "EUR"
        assert s.crypto_agent.pairs and all(p.endswith("/EUR") for p in s.crypto_agent.pairs)

    def test_pairs_must_match_quote_currency(self) -> None:
        from src.core.config import AgentConfig

        with pytest.raises(ValueError, match="quote_currency"):
            AgentConfig(enabled=True, pairs=["BTC/EUR", "ETH/USDT"], quote_currency="EUR")
        ok = AgentConfig(enabled=True, pairs=["BTC/EUR"], quote_currency="eur")
        assert ok.quote_currency == "EUR"

    def test_stocks_agent_disabled_by_default(self, config_path: str) -> None:
        s = Settings(config_path=config_path)

        assert s.stocks_agent.enabled is False

    def test_stocks_window_matches_the_traded_symbols(self, config_path: str) -> None:
        """§7.68: US symbols ⇒ the NYSE window in America/New_York, not Warsaw."""
        from datetime import date

        from src.agents.stocks_agent import parse_holidays

        s = Settings(config_path=config_path)
        cfg = s.stocks_agent

        assert cfg.market_hours == "09:30-16:00"
        assert cfg.market_timezone == "America/New_York"
        # Shipped symbols are US-listed — the window claim rests on that.
        assert all("/" not in sym for sym in cfg.symbols)
        holidays = parse_holidays(cfg.market_holidays)
        assert date(2026, 11, 26) in holidays  # Thanksgiving
        assert date(2027, 3, 26) in holidays  # Good Friday

    def test_env_override_endpoint(self, config_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LM_STUDIO_ENDPOINT", "http://custom:9999/v1/chat/completions")
        s = Settings(config_path=config_path)

        assert s.llm.endpoint == "http://custom:9999/v1/chat/completions"

    def test_fallback_when_no_env(self, config_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LM_STUDIO_ENDPOINT", raising=False)
        s = Settings(config_path=config_path)

        assert "127.0.0.1" in s.llm.endpoint


class TestExecutionSettings:
    def test_execution_settings_loaded_from_yaml(self, config_path: str) -> None:
        s = Settings(config_path=config_path)

        # §7.65: the shipped default mirrors OKX EU spot base tier (taker 0.10%).
        assert s.execution.paper_fee_pct == 0.001
        assert s.execution.paper_slippage_pct == 0.001
        assert s.execution.paper_min_commission == 0.0
        assert s.execution.paper_fx_fee_pct == 0.0

    def test_execution_defaults_when_key_absent(self, app_settings: Settings) -> None:
        # The app_settings fixture builds a config with no `execution` block,
        # so the defaults must kick in (fee-free by default).
        assert app_settings.execution.paper_fee_pct == 0.0
        assert app_settings.execution.paper_slippage_pct == 0.001


class TestPaperCostProfiles:
    """§7.65: per-venue cost profiles under ``execution.paper_costs``."""

    def test_shipped_stocks_profile_matches_saxo_us_pricing(self, config_path: str) -> None:
        s = Settings(config_path=config_path)

        crypto = s.execution.paper_cost_params("crypto")
        assert crypto["paper_fee_pct"] == 0.001  # OKX EU taker, no minimum
        assert crypto["paper_min_commission"] == 0.0

        stocks = s.execution.paper_cost_params("stocks")
        assert stocks["paper_fee_pct"] == 0.0008
        assert stocks["paper_min_commission"] == 1.0
        assert stocks["paper_fx_fee_pct"] == 0.0025

    def test_profile_merges_over_flat_defaults(self) -> None:
        from src.core.config import ExecutionSettings

        e = ExecutionSettings(
            paper_fee_pct=0.01,
            paper_slippage_pct=0.002,
            paper_costs={"stocks": {"paper_fee_pct": 0.0008}},
        )
        params = e.paper_cost_params("stocks")
        assert params["paper_fee_pct"] == 0.0008  # overridden
        assert params["paper_slippage_pct"] == 0.002  # inherited
        # Unknown agents (and unbound lookups) get the flat defaults unchanged.
        assert e.paper_cost_params("unknown")["paper_fee_pct"] == 0.01
        assert e.paper_cost_params()["paper_fee_pct"] == 0.01

    def test_unknown_field_rejected(self) -> None:
        from src.core.config import ExecutionSettings

        with pytest.raises(ValueError, match="unknown field"):
            ExecutionSettings(paper_costs={"crypto": {"initial_cash": 5}})

    def test_negative_cost_rejected(self) -> None:
        from src.core.config import ExecutionSettings

        with pytest.raises(ValueError, match="non-negative"):
            ExecutionSettings(paper_costs={"crypto": {"paper_fee_pct": -0.1}})


class TestRiskSettingsDefaults:
    def test_all_fields_present(self, config_path: str) -> None:
        s = Settings(config_path=config_path)
        r = s.risk

        assert r.max_position_pct == 0.10
        assert r.daily_loss_limit_pct == 0.02
        assert r.max_drawdown_pct == 0.05
        assert r.consecutive_losses_cooldown_minutes == 60
        assert r.max_open_positions == 5
        assert r.min_confidence == 0.6


class TestXTBSymbolMap:
    """§7.59 L8: the data → xAPI symbol table is validated at startup."""

    def test_default_is_empty(self) -> None:
        from src.core.config import XTBExecutionSettings

        assert XTBExecutionSettings().symbol_map == {}

    def test_valid_map_is_kept(self) -> None:
        from src.core.config import XTBExecutionSettings

        cfg = XTBExecutionSettings(symbol_map={"AAPL": "AAPL.US", "MSFT": "MSFT.US"})
        assert cfg.symbol_map == {"AAPL": "AAPL.US", "MSFT": "MSFT.US"}

    @pytest.mark.parametrize(
        "bad",
        [{"AAPL": "X.US", "MSFT": "X.US"}, {"AAPL": ""}, {"AAPL": 5}],
    )
    def test_invalid_maps_fail_fast(self, bad: dict) -> None:
        from src.core.config import XTBExecutionSettings

        with pytest.raises(ValueError, match="symbol_map"):
            XTBExecutionSettings(symbol_map=bad)

    def test_shipped_config_loads(self) -> None:
        from src.core.config import Settings

        assert Settings().xtb_execution.symbol_map == {}
