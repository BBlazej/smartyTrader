"""Entry point: run the stocks agent in paper or XTB-demo mode.

Shared lifecycle (enabled gate, storage/LLM/risk wiring, drawdown seeding, rehydration,
retention pruning, ``--once`` vs scheduled loop, guaranteed cleanup) lives in
:func:`src.core.runner.run_agent` (§7.13). This script keeps only the stocks-specific
wiring: yfinance data + paper execution, with the XTB demo xAPI seam documented below.

External blocker
----------------
XTB's xAPI requires an **approved demo account** and an **OAuth2 flow** (see
``PLAN.md``). The :class:`XTBExecutor` is the execution seam; until a real xAPI client
is provided, this runner keeps the paper executor as the default and logs a clear
notice when XTB credentials are present but no xAPI client is wired yet.

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
from src.core.config import Settings
from src.core.runner import build_alerts, load_dotenv, run_agent
from src.data.xtb_provider import create_xtb_provider
from src.execution.paper_executor import PaperExecutor
from src.monitoring import setup_logging


def _make_components(settings: Settings) -> tuple[object, object]:
    """Build the yfinance provider + paper executor (XTB demo is still blocked).

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

    # Execution: paper by default. XTB demo execution needs an approved demo account
    # + OAuth2 (PLAN.md blocker); when credentials are present we log the gap and
    # stay on paper rather than fail.
    if os.getenv("XTB_API_KEY"):
        log.warning(
            "XTB_API_KEY set but no xAPI client wired yet — using paper executor "
            "until the XTB demo OAuth2 flow lands"
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
        build_components=lambda: _make_components(settings),
        build_agent=lambda pipeline, storage, risk_engine, llm_client: StocksAgent(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=llm_client,
            symbols=settings.stocks_agent.symbols,
            timeframe="1d",
            market_hours=settings.stocks_agent.market_hours or "09:00-16:30",
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


if __name__ == "__main__":
    main()
