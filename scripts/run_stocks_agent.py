"""Entry point: run the stocks agent in paper or XTB-demo mode.

Wires the shared core together from ``config/settings.yaml`` and starts the
scheduled decision loop. The **first cycle runs immediately at startup**, then
repeats every ``stocks_agent.interval_minutes`` (so a decision is produced
without waiting a full interval for the first tick).

Safe by default — the paper executor is used unless an XTB
demo xAPI client is available.

External blocker
----------------
XTB's xAPI requires an **approved demo account** and an **OAuth2 flow** (see
``PLAN.md``). The :class:`XTBExecutor` is the execution seam; until a real xAPI client
is provided, this runner keeps the paper executor as the default and logs a clear
notice when XTB credentials are present but no xAPI client is wired yet.

``stocks_agent.enabled: false`` means **do nothing**: the runner exits before
constructing any component — no cycles, LLM calls, order placement or DB
writes. For an intentional single-cycle run (e.g. cron), pass ``--once``,
which runs exactly one full cycle and exits cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import structlog

from src.agents.stocks_agent import DEFAULT_MARKET_TIMEZONE, StocksAgent
from src.core.config import Settings
from src.core.decision_pipeline import DecisionPipeline
from src.core.llm_client import LLMClient
from src.core.risk_engine import RiskEngine
from src.core.scheduler import create_async_scheduler
from src.core.storage import Storage
from src.data.xtb_provider import create_xtb_provider
from src.execution.paper_executor import PaperExecutor
from src.monitoring import AlertManager, setup_logging


def _load_dotenv(path: str = ".env") -> None:
    """Populate ``os.environ`` from a ``.env`` file (dependency-free).

    Parses simple ``KEY=VALUE`` lines; ignores blanks, comments, and any already-set
    variables so the real environment always wins.
    """
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _build_alerts(settings: Settings) -> AlertManager:
    """Build the alert manager from config (noop sink — logging only)."""
    return AlertManager(dedup_window=float(settings.monitoring.alert_dedup_window_seconds))


async def run(run_once: bool = False) -> None:
    _load_dotenv()
    settings = Settings()
    setup_logging(settings.monitoring.log_level)
    log = structlog.get_logger().bind(component="runner")

    # "disabled" must mean *nothing happens*: exit before constructing any
    # component so no cycle, LLM call, order placement or DB write can occur.
    # Single-cycle runs are an explicit choice via --once, never a side effect
    # of disabling the agent.
    if not settings.stocks_agent.enabled:
        log.warning(
            "stocks agent disabled in config (stocks_agent.enabled: false); "
            "exiting without running anything"
        )
        return

    storage = Storage(settings.storage.database_path)
    await storage.initialize()

    llm_client = LLMClient(settings.llm)
    risk_engine = RiskEngine(settings.risk)

    # Data feed: yfinance-backed provider (OHLCV → MarketSnapshot).
    provider = create_xtb_provider()

    # Execution: paper by default. XTB demo execution needs an approved demo account
    # + OAuth2 (PLAN.md blocker); when credentials are present we log the gap and
    # stay on paper rather than fail.
    if os.getenv("XTB_API_KEY"):
        log.warning(
            "XTB_API_KEY set but no xAPI client wired yet — using paper executor "
            "until the XTB demo OAuth2 flow lands"
        )
    executor = PaperExecutor(
        slippage_pct=settings.execution.paper_slippage_pct,
        fee_pct=settings.execution.paper_fee_pct,
    )
    log.info("using paper executor")

    pipeline = DecisionPipeline(
        provider=provider,
        llm_client=llm_client,
        risk_engine=risk_engine,
        executor=executor,
        storage=storage,
        decision_history_limit=settings.stocks_agent.decision_history_limit,
    )

    agent = StocksAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=risk_engine,
        llm_client=llm_client,
        symbols=settings.stocks_agent.symbols,
        timeframe="1d",
        market_hours=settings.stocks_agent.market_hours or "09:00-16:30",
        market_timezone=settings.stocks_agent.market_timezone or DEFAULT_MARKET_TIMEZONE,
        alerts=_build_alerts(settings),
    )

    if run_once:
        # Explicit single-cycle mode: one full cycle, then a clean shutdown.
        # A failing cycle propagates so the operator sees a non-zero exit code.
        log.info("running a single cycle (--once) then exiting")
        try:
            await agent.run_cycle()
        finally:
            await agent.shutdown()
            await provider.close()
            await executor.close()
            await storage.close()
        return

    from src.core.scheduler import AsyncSchedulerManager

    manager = AsyncSchedulerManager(create_async_scheduler())
    manager.schedule_cycle(
        agent.run_cycle, settings.stocks_agent.interval_minutes, job_id="stocks_cycle"
    )

    await agent.start()
    manager.start()
    log.info("stocks agent running; Ctrl+C to stop")
    try:
        # Run the first cycle immediately so a decision is produced without
        # waiting a full interval for the first scheduled tick. Fail-soft: a
        # bad first cycle must not kill the scheduled loop (which would retry).
        try:
            await agent.run_cycle()
        except Exception as exc:  # noqa: BLE001
            log.warning("initial cycle failed; scheduled cycles will continue", error=str(exc))
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        manager.shutdown()
        await agent.shutdown()
        # Release the data provider / executor (the yfinance source holds no
        # session today, but keep the shutdown path uniform with the crypto runner).
        await provider.close()
        await executor.close()
        await storage.close()
        log.info("stocks agent shut down cleanly")


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
