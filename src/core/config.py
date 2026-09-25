"""Configuration loading from YAML + environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

#: Environment acknowledgement required, together with an explicit config flag,
#: before any executor may touch a REAL-money account (§7.41): a keyed exchange run
#: with ``testnet: false`` and ``xtb_execution.account_type: real``.
LIVE_TRADING_ACK_ENV = "LIVE_TRADING_ACK"
LIVE_TRADING_ACK_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"


def live_trading_acknowledged() -> bool:
    """True only when ``LIVE_TRADING_ACK`` holds the exact acknowledgement phrase."""
    return os.getenv(LIVE_TRADING_ACK_ENV, "").strip() == LIVE_TRADING_ACK_PHRASE


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
        # Untouched copy of the YAML risk limits (§7.43): safe-config overrides may
        # only *tighten* relative to these. ``self.risk`` itself is mutated in place
        # by applied overrides, so it cannot serve as the baseline.
        self.risk_baseline = RiskSettings(**raw["risk"])
        # Untouched YAML copies for the same reason (§7.50): safe-config overrides are
        # applied as *baseline + override* every time, so removing an override reverts
        # the live object instead of leaving a stale value pinned forever.
        self.agent_baselines: dict[str, AgentConfig] = {
            "crypto": AgentConfig(**raw["crypto_agent"]),
            "stocks": AgentConfig(**raw["stocks_agent"]),
        }
        self.execution = ExecutionSettings(**raw.get("execution", {}))
        self.execution_baseline = ExecutionSettings(**raw.get("execution", {}))
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
        # Sampling knobs were hardcoded in the client ("config-driven" rule, §7.19).
        temperature: float = 0.2,
        max_tokens: int = 1024,
        # Base delay for the exponential backoff between retry attempts; 0 disables
        # sleeping (used by tests).
        retry_backoff_base_seconds: float = 1.0,
        # Deterministic-evaluation seed (§7.33): sent with every request when set
        # (models that support it reproduce outputs; LM Studio honors `seed`).
        # None omits the field entirely — provider default behavior.
        seed: int | None = None,
        # Upper bound on a raw completion's character count before parsing (§7.33):
        # a runaway/degenerate generation is treated as a failed attempt, never
        # fed into the signal parser.
        max_response_chars: int = 20_000,
    ) -> None:
        env_endpoint = os.getenv("LM_STUDIO_ENDPOINT")
        self.endpoint = env_endpoint or endpoint
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.seed = seed
        self.max_response_chars = max_response_chars
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
        # Candle timeframe the agent decides on (§7.56) — was hardcoded per agent.
        # None → the agent's default (crypto "1h", stocks "1d").
        timeframe: str | None = None,
        # §7.56: ask the LLM once per newly closed bar; cycles in between only mark
        # positions and enforce exit levels.
        decide_on_new_bar_only: bool = True,
        # §7.41: explicit opt-in for a keyed executor on the live venue (real money).
        # Also needs LIVE_TRADING_ACK in the env.
        live_trading: bool = False,
        # §7.64: currency the keyed executor counts as cash (e.g. "EUR" on OKX Europe,
        # where USDT is not tradable for EEA accounts). Every pair must be quoted in it.
        quote_currency: str | None = None,
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
        self.timeframe = timeframe
        self.decide_on_new_bar_only = decide_on_new_bar_only
        self.live_trading = bool(live_trading)
        self.quote_currency = quote_currency.upper() if quote_currency else None
        if self.quote_currency:
            mismatched = [
                pair for pair in self.pairs if pair.split("/")[-1].upper() != self.quote_currency
            ]
            if mismatched:
                raise ValueError(
                    f"pairs {mismatched} are not quoted in quote_currency "
                    f"'{self.quote_currency}' — cash and position sizing would be wrong"
                )


class RiskSettings:
    def __init__(
        self,
        max_position_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.02,
        max_drawdown_pct: float = 0.05,
        consecutive_losses_cooldown_minutes: int = 60,
        # Streak that arms the cooldown; was hardcoded to 3 in the tracker (§7.19).
        consecutive_losses_threshold: int = 3,
        max_open_positions: int = 5,
        min_confidence: float = 0.6,
        # §7.54 entry geometry: a BUY's stop must sit BELOW the current price and
        # within this fraction of it — an SL at 0.01 means unbounded risk, and stops
        # beyond the bar would auto-close next cycle for double fees.
        max_stop_distance_pct: float = 0.25,
        # §7.54 optional risk-per-trade sizing: cap a BUY so that
        # (entry − stop) × quantity ≤ this fraction of total value. 0 disables it.
        risk_per_trade_pct: float = 0.0,
        # Deterministic stop-loss / take-profit enforcement (§7.9): when enabled,
        # the pipeline closes a position as soon as its mark price breaches the
        # levels carried from the entry signal, without asking the LLM.
        enforce_exit_levels: bool = True,
    ) -> None:
        self.max_position_pct = max_position_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.consecutive_losses_cooldown_minutes = consecutive_losses_cooldown_minutes
        self.consecutive_losses_threshold = consecutive_losses_threshold
        self.max_open_positions = max_open_positions
        self.min_confidence = min_confidence
        self.max_stop_distance_pct = max_stop_distance_pct
        self.risk_per_trade_pct = risk_per_trade_pct
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
        # Point-in-time DB backups ahead of every prune pass (§7.35). Empty string
        # disables them; otherwise each pass writes ``<dir>/<db-stem>-<UTC stamp>.db``
        # via SQLite's online backup API BEFORE any rows are deleted.
        backup_dir: str = "",
        # How many backups to keep (oldest rotated out); 0 keeps everything.
        backup_keep: int = 0,
    ) -> None:
        self.database_path = database_path
        self.snapshot_retention_days = snapshot_retention_days
        self.history_retention_days = history_retention_days
        self.prune_interval_minutes = prune_interval_minutes
        self.backup_dir = backup_dir
        self.backup_keep = backup_keep


class MonitoringSettings:
    def __init__(
        self,
        log_level: str = "INFO",
        alert_dedup_window_seconds: int = 300,
        # §7.51 webhook alert channel. The URL itself is a secret (tokens live in
        # it) and comes ONLY from the ALERT_WEBHOOK_URL environment variable.
        alert_webhook_format: str = "json",
        alert_min_severity: str = "warning",
    ) -> None:
        if alert_webhook_format not in ("json", "ntfy"):
            raise ValueError("monitoring.alert_webhook_format must be 'json' or 'ntfy'")
        if alert_min_severity not in ("info", "warning", "error"):
            raise ValueError("monitoring.alert_min_severity must be info, warning or error")
        self.log_level = log_level
        self.alert_dedup_window_seconds = alert_dedup_window_seconds
        self.alert_webhook_format = alert_webhook_format
        self.alert_min_severity = alert_min_severity


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
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.crypto_port = crypto_port
        self.stocks_port = stocks_port
        # Extra Host names accepted besides loopback + ``host`` (§7.43 DNS-rebinding
        # guard) — e.g. the compose service name when the dashboard calls in.
        self.allowed_hosts = list(allowed_hosts or [])


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
        symbol_map: dict[str, str] | None = None,
    ) -> None:
        if account_type not in ("demo", "real"):
            raise ValueError("xtb_execution.account_type must be 'demo' or 'real'")
        self.enabled = enabled
        self.host = host
        self.account_type = account_type
        self.request_timeout_seconds = float(request_timeout_seconds)
        # Data → xAPI symbol table (§7.59 L8), e.g. {"AAPL": "AAPL.US"}. Unmapped
        # symbols pass through unchanged. Must be one-to-one so positions map back.
        symbol_map = dict(symbol_map or {})
        for key, value in symbol_map.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value:
                raise ValueError("xtb_execution.symbol_map must map symbol strings to strings")
        if len(set(symbol_map.values())) != len(symbol_map):
            raise ValueError("xtb_execution.symbol_map must be one-to-one")
        self.symbol_map: dict[str, str] = symbol_map


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
        allow_launch: bool = False,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        # HTMX polling interval for the live fragments (health cards, chart).
        self.refresh_seconds = max(1, refresh_seconds)
        # Which control rows to show/control. Defaults to both built-in agents.
        self.agents = agents if agents else ["crypto", "stocks"]
        # §7.24: opt-in process supervision — Start/Stop buttons that spawn/terminate
        # local `scripts.run_<agent>_agent` runners. Off by default; pointless (and
        # confusing) inside docker-compose, where services are managed by compose.
        self.allow_launch = bool(allow_launch)
        # Extra Host names the browser may use besides loopback + ``host`` (§7.43):
        # every request with any other Host header is rejected (DNS rebinding).
        self.allowed_hosts = list(allowed_hosts or [])
