"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import LLMSettings, RiskSettings, Settings


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> str:
    """Return a unique temp path for each test database."""
    return str(tmp_path / "test.db")


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
def llm_settings() -> LLMSettings:
    return LLMSettings(
        endpoint="http://localhost:1234/v1/chat/completions",
        model="qwen3.6-27b-mtp",
        timeout_seconds=5,
        max_retries=2,
    )


@pytest.fixture()
def app_settings(tmp_db_path: str, _tmp_path: Path) -> Settings:
    """Settings pointing to a temp database."""
    import yaml

    config = _tmp_path / "test_config.yaml"
    data = {
        "llm": {
            "endpoint": "http://localhost:1234/v1/chat/completions",
            "model": "qwen3.6-27b-mtp",
            "timeout_seconds": 5,
            "max_retries": 2,
        },
        "crypto_agent": {
            "enabled": True,
            "exchange": "kraken",
            "testnet": True,
            "interval_minutes": 5,
            "pairs": ["BTC/USDT"],
        },
        "stocks_agent": {
            "enabled": False,
            "broker": "xtb",
            "demo": True,
            "interval_minutes": 15,
            "symbols": [],
        },
        "risk": {
            "max_position_pct": 0.10,
            "daily_loss_limit_pct": 0.02,
            "max_drawdown_pct": 0.05,
            "consecutive_losses_cooldown_minutes": 60,
            "max_open_positions": 5,
            "min_confidence": 0.6,
        },
        "storage": {
            "database_path": tmp_db_path,
        },
        "monitoring": {
            "log_level": "DEBUG",
            "telegram_enabled": False,
        },
    }

    with open(config, "w") as f:
        yaml.dump(data, f)

    return Settings(config_path=str(config))


# tmp_path is provided by pytest natively — re-export for convenience in async fixtures
@pytest.fixture()
def _tmp_path(tmp_path: Path) -> Path:
    return tmp_path
