"""Entry point: run the crypto agent in paper or Kraken-testnet mode.

Wires the shared core together from ``config/settings.yaml`` and starts the
scheduled decision loop. The **first cycle runs immediately at startup**, then
repeats every ``crypto_agent.interval_minutes`` (so a decision is produced
without waiting a full interval for the first tick).

The **data feed is always live public market data**
(Kraken OHLCV — public endpoints need no API key), so even the paper path
generates real snapshots, indicators, and LLM signals. Execution is safe by
default — the paper executor is used unless ``KRAKEN_API_KEY``/
``KRAKEN_API_SECRET`` are set (in the environment or ``.env``).

``crypto_agent.enabled: false`` means **do nothing**: the runner exits before
constructing any component — no cycles, LLM calls, order placement or DB
writes. For an intentional single-cycle run (e.g. cron), pass ``--once``,
which runs exactly one full cycle and exits cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

import structlog

from src.agents.crypto_agent import CryptoAgent
from src.core.config import Settings
from src.core.decision_pipeline import DecisionPipeline
from src.core.llm_client import LLMClient
from src.core.rehydration import rehydrate_from_storage
from src.core.risk_engine import RiskEngine
from src.core.scheduler import create_async_scheduler
from src.core.storage import Storage
from src.data.ccxt_provider import create_ccxt_provider
from src.execution.kraken_executor import create_kraken_executor
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


def _build_data_and_execution(settings: Settings) -> tuple[Any, Any, str]:
    """Select the market-data provider and the executor.

    The data provider always uses **live public market data** — Kraken's public
    OHLCV endpoint needs no API key and no sandbox mode, so both the paper and
    the testnet paths are driven by real prices. This is what lets paper mode
    learn from and store real snapshots instead of the old empty-candles mock.

    Execution is safe by default:

    * No API key → :class:`PaperExecutor` (simulated fills, fee + slippage aware).
    * API key set → :class:`KrakenExecutor` against a separate, sandboxed,
      keyed client (order placement only).

    Returns ``(provider, executor, mode)`` where ``mode`` is ``"paper"`` or
    ``"kraken-testnet"``.
    """
    exchange = settings.crypto_agent.exchange or "kraken"
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_API_SECRET")

    # Data feed — real public OHLCV. Public data is read-only, so no sandbox mode.
    # ``create_ccxt_provider`` imports ccxt lazily; if it is missing, fail fast
    # with an actionable message rather than surfacing a per-cycle fetch error.
    try:
        provider = create_ccxt_provider(exchange_id=exchange, testnet=False)
    except ImportError as exc:
        raise SystemExit(
            "The crypto agent needs the `ccxt` package to fetch live market data "
            "(even in paper mode). Install it with: pip install ccxt"
        ) from exc

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


async def run(run_once: bool = False) -> None:
    _load_dotenv()
    settings = Settings()
    setup_logging(settings.monitoring.log_level)
    log = structlog.get_logger().bind(component="runner")

    # "disabled" must mean *nothing happens*: exit before constructing any
    # component so no cycle, LLM call, order placement or DB write can occur.
    # Single-cycle runs are an explicit choice via --once, never a side effect
    # of disabling the agent.
    if not settings.crypto_agent.enabled:
        log.warning(
            "crypto agent disabled in config (crypto_agent.enabled: false); "
            "exiting without running anything"
        )
        return

    storage = Storage(settings.storage.database_path)
    await storage.initialize()

    llm_client = LLMClient(settings.llm)
    risk_engine = RiskEngine(settings.risk)

    # Seed the drawdown high-water mark from persisted portfolio history so a
    # restart cannot reset the guard (§7.5). Fail-soft: without history the
    # engine seeds lazily from the first reading.
    try:
        risk_engine.seed_peak_equity(await storage.get_max_portfolio_value())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not seed drawdown peak from storage; starting fresh", error=str(exc))

    # Data + execution. Live public data in both modes; safe paper execution by
    # default, Kraken testnet only when an API key is provided.
    provider, executor, mode = _build_data_and_execution(settings)
    if mode == "paper":
        log.info(
            "using live public data + paper executor (no KRAKEN_API_KEY set)",
            fee_pct=settings.execution.paper_fee_pct,
            slippage_pct=settings.execution.paper_slippage_pct,
        )
    else:
        log.info("using live public data + Kraken testnet executor")

    # Rebuild the paper book and risk trackers from persisted state so a
    # restart never silently resets cash, positions or the loss guards (§7.7).
    await rehydrate_from_storage(risk_engine, executor, storage)

    pipeline = DecisionPipeline(
        provider=provider,
        llm_client=llm_client,
        risk_engine=risk_engine,
        executor=executor,
        storage=storage,
        decision_history_limit=settings.crypto_agent.decision_history_limit,
    )

    agent = CryptoAgent(
        pipeline=pipeline,
        storage=storage,
        risk_engine=risk_engine,
        llm_client=llm_client,
        pairs=settings.crypto_agent.pairs,
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
        agent.run_cycle, settings.crypto_agent.interval_minutes, job_id="crypto_cycle"
    )

    await agent.start()
    manager.start()
    log.info("crypto agent running; Ctrl+C to stop")
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
        # Release the exchange sessions (data feed + order client, the latter only
        # in testnet mode). CCXT's async exchange owns an aiohttp session that
        # must be closed explicitly, or it leaks on exit.
        await provider.close()
        await executor.close()
        await storage.close()
        log.info("crypto agent shut down cleanly")


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
