"""Entry point: run the stocks agent in paper or XTB-demo mode.

Shared lifecycle (enabled gate, storage/LLM/risk wiring, drawdown seeding, rehydration,
retention pruning, ``--once`` vs scheduled loop, guaranteed cleanup) lives in
:func:`src.core.runner.run_agent` (§7.13). This script keeps only the stocks-specific
wiring: yfinance data + executor selection (paper default, XTB demo opt-in).

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
from src.core.runner import RunnerAlreadyRunning, build_alerts, load_dotenv, run_agent
from src.data.xtb_provider import create_xtb_provider
from src.execution.paper_executor import PaperExecutor
from src.execution.xtb_client import XApiClient
from src.execution.xtb_executor import XTBExecutor
from src.monitoring import setup_logging


def _make_components(settings: Settings) -> tuple[object, object]:
    """Build the yfinance provider + executor (paper default; XTB demo opt-in).

    ``create_xtb_provider`` checks for yfinance eagerly; if it is missing, fail
    fast with an actionable message rather than surfacing a per-cycle fetch
    error (mirrors the ccxt hint in the crypto runner).
    """
    log = structlog.get_logger().bind(component="runner")
    try:
        provider = create_xtb_provider()
    except ImportError as exc:
        raise SystemExit(
            "The stocks agent needs the `yfinance` package to fetch market data. "
            "Install it with: pip install yfinance   (or: pip install -e '.[stocks]')"
        ) from exc

    # Execution (§7.16): paper unless xtb_execution.enabled AND both env credentials
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

    executor = PaperExecutor(
        initial_cash=settings.execution.initial_cash,
        slippage_pct=settings.execution.paper_slippage_pct,
        fee_pct=settings.execution.paper_fee_pct,
    )
    log.info("using paper executor")
    return provider, executor


async def run(run_once: bool = False) -> None:
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
    args = parser.parse_args()

    try:
        asyncio.run(run(run_once=args.once))
    except KeyboardInterrupt:
        pass
    except RunnerAlreadyRunning:
        # §7.52: another runner owns this agent; run_agent logged the reason.
        # Distinct exit code so cron/systemd notices a refused double-start.
        raise SystemExit(2)


if __name__ == "__main__":
    main()
