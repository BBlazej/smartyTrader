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

    def test_crypto_agent_config(self, config_path: str) -> None:
        s = Settings(config_path=config_path)

        assert s.crypto_agent.enabled is True
        assert "BTC/USDT" in s.crypto_agent.pairs

    def test_stocks_agent_disabled_by_default(self, config_path: str) -> None:
        s = Settings(config_path=config_path)

        assert s.stocks_agent.enabled is False

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

        assert s.execution.paper_fee_pct == 0.0026
        assert s.execution.paper_slippage_pct == 0.001

    def test_execution_defaults_when_key_absent(self, app_settings: Settings) -> None:
        # The app_settings fixture builds a config with no `execution` block,
        # so the defaults must kick in (fee-free by default).
        assert app_settings.execution.paper_fee_pct == 0.0
        assert app_settings.execution.paper_slippage_pct == 0.001


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
