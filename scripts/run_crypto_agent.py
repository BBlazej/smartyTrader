"""Entry point: run the crypto agent in paper or Kraken-testnet mode.

Shared lifecycle (enabled gate, storage/LLM/risk wiring, drawdown seeding, rehydration,
retention pruning, ``--once`` vs scheduled loop, guaranteed cleanup) lives in
:func:`src.core.runner.run_agent` (§7.13). This script keeps only the crypto-specific
wiring: live public CCXT data + paper execution by default, Kraken testnet execution
when ``KRAKEN_API_KEY`` is set.

``crypto_agent.enabled: false`` means **do nothing**: the runner exits before
constructing any component — no cycles, LLM calls, order placement or DB writes.
For an intentional single-cycle run (e.g. cron), pass ``--once``, which runs exactly
one full cycle and exits cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import structlog

from src.agents.crypto_agent import CryptoAgent
from src.core.config import Settings
from src.core.runner import build_alerts, load_dotenv, run_agent
from src.data.ccxt_provider import create_ccxt_provider
from src.execution.kraken_executor import create_kraken_executor
from src.execution.paper_executor import PaperExecutor
from src.monitoring import setup_logging


def _build_data_and_execution(settings: Settings) -> tuple[object, object, str]:
    """Build the (provider, executor) pair plus a mode label.

    The **data feed is always live public market data** (Kraken's public OHLCV
    endpoint needs no API key and no sandbox mode). Only *execution* changes:

    - no ``KRAKEN_API_KEY`` → :class:`PaperExecutor` (safe default);
    - key set → :class:`KrakenExecutor` on a separate, keyed, sandboxed client.
    """
    exchange = settings.crypto_agent.exchange or "kraken"
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_API_SECRET")

    provider = create_ccxt_provider(exchange_id=exchange, testnet=False)

    if api_key:
        # A dedicated, sandboxed, keyed client for order placement (public data
        # above intentionally stays on the main endpoint).
        order_client = create_ccxt_provider(
            exchange_id=exchange,
            testnet=settings.crypto_agent.testnet,
            api_key=api_key,
            api_secret=api_secret,
        ).client
        executor = create_kraken_executor(order_client)
        return provider, executor, "kraken-testnet"

    executor = PaperExecutor(
        initial_cash=settings.execution.initial_cash,
        slippage_pct=settings.execution.paper_slippage_pct,
        fee_pct=settings.execution.paper_fee_pct,
    )
    return provider, executor, "paper"


def _make_components(settings: Settings) -> tuple[object, object]:
    """Build provider + executor and log which mode was chosen."""
    provider, executor, mode = _build_data_and_execution(settings)
    log = structlog.get_logger().bind(component="runner")
    if mode == "paper":
        log.info(
            "using live public data + paper executor (no KRAKEN_API_KEY set)",
            fee_pct=settings.execution.paper_fee_pct,
            slippage_pct=settings.execution.paper_slippage_pct,
        )
    else:
        log.info("using live public data + Kraken testnet executor")
    return provider, executor


async def run(run_once: bool = False) -> None:
    load_dotenv()
    settings = Settings()
    setup_logging(settings.monitoring.log_level)

    await run_agent(
        settings,
        component="crypto",
        agent_enabled=settings.crypto_agent.enabled,
        interval_minutes=settings.crypto_agent.interval_minutes,
        decision_history_limit=settings.crypto_agent.decision_history_limit,
        job_id="crypto_cycle",
        timeframe=settings.crypto_agent.timeframe or "1h",
        decide_on_new_bar_only=settings.crypto_agent.decide_on_new_bar_only,
        build_components=lambda: _make_components(settings),
        build_agent=lambda pipeline, storage, risk_engine, llm_client: CryptoAgent(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=llm_client,
            pairs=settings.crypto_agent.pairs,
            timeframe=settings.crypto_agent.timeframe or "1h",
            alerts=build_alerts(settings),
        ),
        run_once=run_once,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the crypto agent on a schedule. A disabled agent "
            "(crypto_agent.enabled: false) exits without running anything."
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
