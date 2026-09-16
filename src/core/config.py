"""Configuration loading from YAML + environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


class Settings:
    """Application settings loaded from config/settings.yaml and .env."""

    def __init__(self, config_path: str | None = None) -> None:
        path = (
            Path(config_path)
            if config_path
            else Path(__file__).parents[2] / "config" / "settings.yaml"
        )
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f)

        self.llm = LLMSettings(**raw["llm"])
        self.crypto_agent = AgentConfig(**raw["crypto_agent"])
        self.stocks_agent = AgentConfig(**raw["stocks_agent"])
        self.risk = RiskSettings(**raw["risk"])
        self.execution = ExecutionSettings(**raw.get("execution", {}))
        self.storage = StorageSettings(**raw["storage"])
        self.monitoring = MonitoringSettings(**raw["monitoring"])


class LLMSettings:
    def __init__(
        self,
        endpoint: str,
        model: str,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        use_json_schema: bool = False,
    ) -> None:
        env_endpoint = os.getenv("LM_STUDIO_ENDPOINT")
        self.endpoint = env_endpoint or endpoint
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        # Opt-in: request a strict JSON response schema. Enable once you've
        # confirmed the local model supports ``response_format`` — some setups
        # reject it, which would otherwise force the safe HOLD fallback every cycle.
        self.use_json_schema = use_json_schema or os.getenv(
            "LM_STUDIO_USE_JSON_SCHEMA", ""
        ).lower() in ("1", "true", "yes")


class AgentConfig:
    def __init__(
        self,
        enabled: bool,
        exchange: str | None = None,
        broker: str | None = None,
        testnet: bool = True,
        demo: bool = True,
        interval_minutes: int = 5,
        pairs: list[str] | None = None,
        symbols: list[str] | None = None,
        market_hours: str | None = None,
        market_timezone: str | None = None,
        decision_history_limit: int = 10,
    ) -> None:
        self.enabled = enabled
        self.exchange = exchange
        self.broker = broker
        self.testnet = testnet
        self.demo = demo
        self.interval_minutes = interval_minutes
        self.pairs = pairs or []
        self.symbols = symbols or []
        self.market_hours = market_hours
        # IANA zone for the market-hours guard (e.g. "Europe/Warsaw"). The window
        # is a local wall-clock range, so ``now`` is rendered in this zone before
        # comparison — a UTC host otherwise runs the guard 1–2h off. Falls back
        # to the agent's default zone when unset.
        self.market_timezone = market_timezone
        # How many of this agent's prior decisions to feed back into the LLM
        # prompt ("learn from its own track record"). 0 disables the section.
        self.decision_history_limit = decision_history_limit


class RiskSettings:
    def __init__(
        self,
        max_position_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.02,
        max_drawdown_pct: float = 0.05,
        consecutive_losses_cooldown_minutes: int = 60,
        max_open_positions: int = 5,
        min_confidence: float = 0.6,
    ) -> None:
        self.max_position_pct = max_position_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.consecutive_losses_cooldown_minutes = consecutive_losses_cooldown_minutes
        self.max_open_positions = max_open_positions
        self.min_confidence = min_confidence


class ExecutionSettings:
    """Parameters for the simulated (paper) executor.

    Kept config-driven rather than hardcoded so paper PnL — which the LLM is
    shown and the live-readiness gates are compared against — reflects real
    trading costs. Set both to ``0`` to model a fee-free, slip-free venue.
    """

    def __init__(
        self,
        paper_fee_pct: float = 0.0,
        paper_slippage_pct: float = 0.001,  # per-side slippage (0.1%)
        initial_cash: float = 100_000.0,
    ) -> None:
        self.paper_fee_pct = paper_fee_pct
        self.paper_slippage_pct = paper_slippage_pct
        # Starting bankroll for a *fresh* paper portfolio (§7.7: was hardcoded
        # in PaperExecutor). After the first cycle the persisted portfolio
        # snapshot wins — this only seeds an empty one.
        self.initial_cash = initial_cash


class StorageSettings:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path


class MonitoringSettings:
    def __init__(
        self,
        log_level: str = "INFO",
        alert_dedup_window_seconds: int = 300,
    ) -> None:
        self.log_level = log_level
        self.alert_dedup_window_seconds = alert_dedup_window_seconds
