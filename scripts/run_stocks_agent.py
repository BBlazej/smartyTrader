"""Entry point: run the stocks agent in paper, Saxo (SIM) or — legacy — XTB-demo mode.

Shared lifecycle (enabled gate, storage/LLM/risk wiring, drawdown seeding, rehydration,
retention pruning, ``--once`` vs scheduled loop, guaranteed cleanup) lives in
:func:`src.core.runner.run_agent` (§7.13). This script keeps only the stocks-specific
wiring: yfinance data + executor selection (paper default, Saxo SIM opt-in).

Saxo execution (§7.66)
----------------------
:class:`src.execution.saxo_executor.SaxoExecutor` is wired when
``saxo_execution.enabled`` and credentials exist: with ``saxo_execution.oauth.enabled``
the OAuth app (env ``SAXO_APP_KEY``/``SAXO_APP_SECRET`` + a login stored by
``python -m scripts.saxo_login``; tokens refresh themselves), otherwise
``SAXO_ACCESS_TOKEN`` (for SIM, the 24 h developer token from the Saxo developer
portal). ``environment: live`` additionally needs ``LIVE_TRADING_ACK`` (§7.41).
Anything missing → paper, with a warning.

XTB demo execution (§7.16)
--------------------------
The real xAPI client (:class:`src.execution.xtb_client.XApiClient`) is wired when
``xtb_execution.enabled`` **and** both ``XTB_ACCOUNT_ID`` + ``XTB_ACCOUNT_PASSWORD``
(the xAPI verification code from xStation, not the login password) are set. Anything
missing → the paper executor stays and a warning explains why. Opting in is
config-and-env only: the dashboard's safe-config whitelist deliberately excludes
this block.

``stocks_agent.enabled: false`` means **do nothing**: the runner exits before
constructing any component — no cycles, LLM calls, order placement or DB writes.
For an intentional single-cycle run (e.g. cron), pass ``--once``, which runs exactly
one full cycle and exits cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import structlog

from src.agents.stocks_agent import DEFAULT_MARKET_TIMEZONE, StocksAgent
from src.core.config import (
    LIVE_TRADING_ACK_ENV,
    LIVE_TRADING_ACK_PHRASE,
    Settings,
    live_trading_acknowledged,
)
from src.core.db_layout import DEMO, PAPER, REAL
from src.core.runner import ModeMismatch, RunnerAlreadyRunning, build_alerts, load_dotenv, run_agent
from src.data.stocks_provider import create_stocks_provider
from src.execution.paper_executor import create_paper_executor
from src.execution.saxo_auth import SaxoAuthError, SaxoOAuth, TokenStore
from src.execution.saxo_client import SaxoClient
from src.execution.saxo_executor import SaxoExecutor
from src.execution.xtb_client import XApiClient
from src.execution.xtb_executor import XTBExecutor
from src.monitoring import setup_logging

#: Saxo OpenAPI bearer token (§7.66) — env only, never YAML, never logged.
SAXO_TOKEN_ENV = "SAXO_ACCESS_TOKEN"
#: Saxo OAuth app credentials (§7.66 step 4) — env only.
SAXO_APP_KEY_ENV = "SAXO_APP_KEY"
SAXO_APP_SECRET_ENV = "SAXO_APP_SECRET"


def _saxo_client(settings: Settings, log: object) -> SaxoClient | None:
    """An authenticated client (OAuth app or developer token), or ``None`` + why."""
    cfg = settings.saxo_execution
    if cfg.oauth.enabled:
        app_key = os.getenv(SAXO_APP_KEY_ENV, "").strip()
        app_secret = os.getenv(SAXO_APP_SECRET_ENV, "").strip()
        if not app_key or not app_secret:
            log.warning(  # type: ignore[attr-defined]
                f"saxo_execution.oauth.enabled but {SAXO_APP_KEY_ENV}/{SAXO_APP_SECRET_ENV} "
                "are unset — staying on the paper executor"
            )
            return None
        store = TokenStore(cfg.oauth.token_path(settings.storage.data_dir, cfg.environment))
        try:
            stored = store.load()
        except SaxoAuthError as exc:
            log.warning(f"{exc} — staying on the paper executor")  # type: ignore[attr-defined]
            return None
        if stored is None:
            log.warning(  # type: ignore[attr-defined]
                f"no Saxo login stored at {store.path} — run `python -m scripts.saxo_login`; "
                "staying on the paper executor"
            )
            return None
        oauth = SaxoOAuth(
            app_key,
            app_secret,
            cfg.oauth.redirect_uri,
            store,
            environment=cfg.environment,
            auth_base_url=cfg.oauth.auth_base_url,
            refresh_margin_seconds=cfg.oauth.refresh_margin_seconds,
            keepalive_minutes=cfg.oauth.keepalive_minutes,
            timeout_seconds=cfg.request_timeout_seconds,
        )
        return SaxoClient(
            token_source=oauth,
            environment=cfg.environment,
            timeout_seconds=cfg.request_timeout_seconds,
        )
    token = os.getenv(SAXO_TOKEN_ENV, "").strip()
    if not token:
        log.warning(  # type: ignore[attr-defined]
            f"saxo_execution.enabled but {SAXO_TOKEN_ENV} is unset — staying on the paper executor"
        )
        return None
    return SaxoClient(
        token, environment=cfg.environment, timeout_seconds=cfg.request_timeout_seconds
    )


def _saxo_executor(settings: Settings, log: object) -> SaxoExecutor | None:
    """The Saxo executor when fully configured, else ``None`` (stay on paper, say why)."""
    cfg = getattr(settings, "saxo_execution", None)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.environment == "live" and not live_trading_acknowledged():
        log.warning(  # type: ignore[attr-defined]
            "saxo_execution.environment is 'live' (REAL money) but live trading is not "
            f"acknowledged — staying on the paper executor. Set "
            f"{LIVE_TRADING_ACK_ENV}={LIVE_TRADING_ACK_PHRASE} to trade the live account."
        )
        return None
    client = _saxo_client(settings, log)
    if client is None:
        return None
    log.info(  # type: ignore[attr-defined]
        "using Saxo executor via OpenAPI",
        environment=cfg.environment,
        url=client.base_url,
        auth="oauth" if cfg.oauth.enabled else "developer token",
    )
    return SaxoExecutor(
        client,
        venue=f"saxo-{cfg.environment}",
        account_key=cfg.account_key,
        account_currency=cfg.account_currency,
        symbol_map=cfg.symbol_map,
        amount_decimals=cfg.amount_decimals,
        fill_poll_delays=cfg.fill_poll_delays,
    )


def _make_components(settings: Settings) -> tuple[object, object]:
    """Build the yfinance provider + executor (paper default; Saxo SIM opt-in; XTB legacy).

    ``create_stocks_provider`` checks for yfinance eagerly; if it is missing, fail
    fast with an actionable message rather than surfacing a per-cycle fetch
    error (mirrors the ccxt hint in the crypto runner).
    """
    log = structlog.get_logger().bind(component="runner")
    try:
        provider = create_stocks_provider()
    except ImportError as exc:
        raise SystemExit(
            "The stocks agent needs the `yfinance` package to fetch market data. "
            "Install it with: pip install yfinance   (or: pip install -e '.[stocks]')"
        ) from exc

    saxo = _saxo_executor(settings, log)
    if saxo is not None:
        return provider, saxo

    # Legacy XTB (§7.16 — DEAD, §7.66): paper unless xtb_execution.enabled AND both env credentials
    # are present. Anything missing keeps the safe default and says why — never a
    # silent half-wired live path.
    xtb_cfg = settings.xtb_execution
    if xtb_cfg.enabled:
        account_id = os.getenv("XTB_ACCOUNT_ID")
        verification_code = os.getenv("XTB_ACCOUNT_PASSWORD")
        if not account_id or not verification_code:
            log.warning(
                "xtb_execution.enabled but XTB_ACCOUNT_ID/XTB_ACCOUNT_PASSWORD are "
                "unset — staying on the paper executor"
            )
        elif xtb_cfg.account_type == "real" and not live_trading_acknowledged():
            # §7.41 / review L7: a one-word YAML edit must never reach a real-money
            # account — it also needs the explicit environment acknowledgement.
            log.warning(
                "xtb_execution.account_type is 'real' (REAL money) but live trading is not "
                f"acknowledged — staying on the paper executor. Set "
                f"{LIVE_TRADING_ACK_ENV}={LIVE_TRADING_ACK_PHRASE} to trade the real account."
            )
        else:
            client = XApiClient(
                account_id,
                verification_code,
                host=xtb_cfg.host,
                account_type=xtb_cfg.account_type,
                timeout_seconds=xtb_cfg.request_timeout_seconds,
            )
            log.info(
                "using XTB executor via xAPI",
                account_type=xtb_cfg.account_type,
                url=client.url,
            )
            return provider, XTBExecutor(
                client,
                venue=f"xtb-{xtb_cfg.account_type}",
                symbol_map=xtb_cfg.symbol_map,
            )

    executor = create_paper_executor(settings, "stocks")
    log.info("using paper executor", **settings.execution.paper_cost_params("stocks"))
    return provider, executor


async def run(run_once: bool = False, expected_mode: str | None = None) -> None:
    load_dotenv()
    settings = Settings()
    setup_logging(settings.monitoring.log_level)

    await run_agent(
        settings,
        component="stocks",
        agent_enabled=settings.stocks_agent.enabled,
        interval_minutes=settings.stocks_agent.interval_minutes,
        decision_history_limit=settings.stocks_agent.decision_history_limit,
        job_id="stocks_cycle",
        timeframe=settings.stocks_agent.timeframe or "1d",
        decide_on_new_bar_only=settings.stocks_agent.decide_on_new_bar_only,
        build_components=lambda: _make_components(settings),
        build_agent=lambda pipeline, storage, risk_engine, llm_client: StocksAgent(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=llm_client,
            symbols=settings.stocks_agent.symbols,
            timeframe=settings.stocks_agent.timeframe or "1d",
            # §7.50: hand the agent the live settings object (the control plane mutates
            # it) instead of a constructor copy — market-hours overrides then apply.
            agent_settings=settings.stocks_agent,
            market_timezone=settings.stocks_agent.market_timezone or DEFAULT_MARKET_TIMEZONE,
            market_holidays=settings.stocks_agent.market_holidays,
            alerts=build_alerts(settings),
        ),
        run_once=run_once,
        expected_mode=expected_mode,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the stocks agent on a schedule. A disabled agent "
            "(stocks_agent.enabled: false) exits without running anything."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one decision cycle and exit instead of the scheduled loop.",
    )
    parser.add_argument(
        "--mode",
        choices=[PAPER, DEMO, REAL],
        default=None,
        help="Refuse to start unless the built executor trades this mode (§7.78): it picks "
        "the book file, so --mode demo guarantees SIM keys and --mode real a live account.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(run_once=args.once, expected_mode=args.mode))
    except KeyboardInterrupt:
        pass
    except RunnerAlreadyRunning:
        # §7.52: another runner owns this agent × mode; run_agent logged the reason.
        # Distinct exit code so cron/systemd notices a refused double-start.
        raise SystemExit(2)
    except ModeMismatch:
        # §7.78: --mode and the executor disagreed; nothing was constructed against a DB.
        raise SystemExit(3)


if __name__ == "__main__":
    main()
