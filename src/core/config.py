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
        self.control_api = ControlApiSettings(**raw.get("control_api", {}))
        self.dashboard = DashboardSettings(**raw.get("dashboard", {}))
        self.xtb_execution = XTBExecutionSettings(**raw.get("xtb_execution", {}))


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
        # Exchange closure dates (§7.10): ISO strings ("YYYY-MM-DD"). Weekends are
        # always closed; this list covers holidays and one-off shutdowns.
        market_holidays: list[str] | None = None,
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
        self.market_holidays = market_holidays or []
        # How many of this agent's prior decisions to feed back into the prompt
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
        # Deterministic stop-loss / take-profit enforcement (§7.9): when enabled,
        # the pipeline closes a position as soon as its mark price breaches the
        # levels carried from the entry signal, without asking the LLM.
        enforce_exit_levels: bool = True,
    ) -> None:
        self.max_position_pct = max_position_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.consecutive_losses_cooldown_minutes = consecutive_losses_cooldown_minutes
        self.max_open_positions = max_open_positions
        self.min_confidence = min_confidence
        self.enforce_exit_levels = enforce_exit_levels


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
    def __init__(
        self,
        database_path: str,
        # Retention windows in days (§7.12). Market snapshots are the space hogs
        # (~100-candle JSON per symbol-cycle) and are re-creatable cache, so they
        # prune by default; decisions/orders are the trade record and kept forever
        # unless explicitly bounded. portfolio_snapshots are never pruned (the
        # drawdown high-water seed reads MAX over their full history).
        snapshot_retention_days: int = 30,
        history_retention_days: int = 0,
        # How often the runners run the pruning job while alive.
        prune_interval_minutes: int = 1440,
    ) -> None:
        self.database_path = database_path
        self.snapshot_retention_days = snapshot_retention_days
        self.history_retention_days = history_retention_days
        self.prune_interval_minutes = prune_interval_minutes


class MonitoringSettings:
    def __init__(
        self,
        log_level: str = "INFO",
        alert_dedup_window_seconds: int = 300,
    ) -> None:
        self.log_level = log_level
        self.alert_dedup_window_seconds = alert_dedup_window_seconds


class ControlApiSettings:
    """Agent-side control API (§7.15 P2): off unless explicitly enabled.

    Binds to loopback by default — the dashboard shares the Docker network (or the
    same host), never the public internet. It exposes only safe config + control
    latches; credentials are structurally absent from every endpoint.
    """

    def __init__(
        self,
        enabled: bool = False,
        host: str = "127.0.0.1",
        crypto_port: int = 8101,
        stocks_port: int = 8102,
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.crypto_port = crypto_port
        self.stocks_port = stocks_port


class XTBExecutionSettings:
    """XTB demo execution via xAPI (§7.16): off unless explicitly enabled.

    When enabled (and ``XTB_ACCOUNT_ID`` + ``XTB_ACCOUNT_PASSWORD`` are set in the
    environment), the stocks runner wires :class:`XTBExecutor` over the real
    :class:`~src.execution.xtb_client.XApiClient` instead of the paper executor.
    Default stays paper. ``account_type`` is validated at startup; keep it on
    ``demo`` — live trading remains out of scope (and this block is deliberately
    **not** in the dashboard's safe-config whitelist: enabling real execution
    must never be a web-form click).
    """

    def __init__(
        self,
        enabled: bool = False,
        host: str = "wss://ws.xapi.pro",
        account_type: str = "demo",
        request_timeout_seconds: float = 10.0,
    ) -> None:
        if account_type not in ("demo", "real"):
            raise ValueError("xtb_execution.account_type must be 'demo' or 'real'")
        self.enabled = enabled
        self.host = host
        self.account_type = account_type
        self.request_timeout_seconds = float(request_timeout_seconds)


class DashboardSettings:
    """Standalone web dashboard (§7.15 P3–P4): FastAPI + Jinja2/HTMX.

    Launched on its own (``scripts/run_dashboard.py``) and reads the shared SQLite
    DB as a reader (WAL mode lets it read while the agents write). Control actions
    write the ``agent_control`` latches directly — the same writes the agent-side
    control API makes — so they work whether or not ``control_api.enabled``.

    Binds to loopback by default; credentials are structurally absent from every
    page. The dashboard only ever shows/edits the safe config surface
    (:class:`~src.core.control_config.SafeConfigOverrides`).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        refresh_seconds: int = 5,
        agents: list[str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        # HTMX polling interval for the live fragments (health cards, chart).
        self.refresh_seconds = max(1, refresh_seconds)
        # Which control rows to show/control. Defaults to both built-in agents.
        self.agents = agents if agents else ["crypto", "stocks"]
