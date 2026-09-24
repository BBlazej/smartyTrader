"""Shared runner wiring for both market agents (§7.13).

Both entry scripts (``scripts/run_crypto_agent.py`` / ``scripts/run_stocks_agent.py``)
previously duplicated ~80 lines each: ``_load_dotenv``, the alert-manager build, the
enabled-gate, storage/LLM/risk construction, drawdown seeding, rehydration + retention
pruning, the pipeline build, the ``--once`` path and the scheduled loop with guaranteed
cleanup. Every fix landed twice (coverage drift visible in the stocks agent); this module
is now the single implementation.

Scripts keep only what is genuinely market-specific: building the *data provider +
executor* pair and constructing their agent — passed in here as callbacks so the shared
lifecycle stays identical while per-venue seams remain patchable per script.

Safety invariants preserved (see §7.2 / AGENTS.md):

* ``agent_enabled=False`` ⇒ return before anything is constructed — no storage init,
  LLM client, provider/executor, pipeline or agent; no cycles, orders or DB writes.
* ``run_once=True`` runs exactly one full cycle with guaranteed cleanup and never
  starts the scheduler; a failing cycle propagates (non-zero exit for cron).
* The scheduled path runs a first cycle immediately (fail-soft) and cleans up all
  components on shutdown via ``try/finally``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from ..monitoring.alerts import AlertManager
from .config import Settings
from .control_config import parse_and_apply
from .decision_pipeline import DecisionPipeline
from .llm_client import LLMClient
from .rehydration import rehydrate_from_storage
from .retention import prune_storage
from .risk_engine import RiskEngine
from .storage import Storage


def load_dotenv(path: str = ".env") -> None:
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


def build_alerts(settings: Settings) -> AlertManager:
    """Build the alert manager from config (noop sink — logging only)."""
    return AlertManager(dedup_window=float(settings.monitoring.alert_dedup_window_seconds))


async def run_agent(
    settings: Settings,
    *,
    component: str,
    agent_enabled: bool,
    interval_minutes: int,
    decision_history_limit: int,
    job_id: str,
    build_components: Callable[[], tuple[Any, Any]],
    build_agent: Callable[[DecisionPipeline, Storage, RiskEngine, LLMClient], Any],
    run_once: bool = False,
) -> None:
    """Shared agent lifecycle for both runners. Returns when the loop exits.

    ``build_components`` must return ``(provider, executor)`` — constructing the
    market-specific data feed and execution adapter stays in the script (and out
    of reach entirely when the agent is disabled). ``build_agent`` receives the
    wired pipeline/storage/risk/LLM and returns the market's agent instance.
    """
    log = structlog.get_logger().bind(component="runner")

    # "disabled" must mean *nothing happens*: return before constructing any
    # component so no cycle, LLM call, order placement or DB write can occur.
    # Single-cycle runs are an explicit choice via --once, never a side effect
    # of disabling the agent.
    if not agent_enabled:
        log.warning(
            f"{component} agent disabled in config ({component}_agent.enabled: false); "
            "exiting without running anything"
        )
        return

    # Agent-bound storage (§7.39): both agents share one DB file, so every write is
    # stamped with this component and every book/decision/order read stays within it.
    storage = Storage(settings.storage.database_path, agent=component)
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

    provider, executor = build_components()

    # Rebuild the paper book and risk trackers from persisted state so a
    # restart never silently resets cash, positions or the loss guards (§7.7).
    await rehydrate_from_storage(risk_engine, executor, storage)

    # Retention pruning (§7.12): one pass at startup — so even --once cron usage
    # stays hygienic — plus a scheduled pass while running (registered below).
    await prune_storage(storage, settings.storage)

    pipeline = DecisionPipeline(
        provider=provider,
        llm_client=llm_client,
        risk_engine=risk_engine,
        executor=executor,
        storage=storage,
        decision_history_limit=decision_history_limit,
    )

    agent = build_agent(pipeline, storage, risk_engine, llm_client)

    # Control plane (§7.15): the agent re-reads its ``agent_control`` row each cycle;
    # stored safe-config overrides land on *these* live objects via the closure below.
    if hasattr(agent, "set_control_overrides_applier"):
        agent.set_control_overrides_applier(
            lambda raw: parse_and_apply(settings, component, raw, pipeline=pipeline, agent=agent)
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

    # Late import so tests can patch src.core.scheduler.AsyncSchedulerManager.
    from .scheduler import AsyncSchedulerManager, create_async_scheduler

    manager = AsyncSchedulerManager(create_async_scheduler())
    manager.schedule_cycle(agent.run_cycle, interval_minutes, job_id=job_id)
    if settings.storage.prune_interval_minutes > 0:
        manager.schedule_cycle(
            lambda: prune_storage(storage, settings.storage),
            settings.storage.prune_interval_minutes,
            job_id="storage_prune",
        )

    # Control API (§7.15 P2): in-process FastAPI server when explicitly enabled.
    # Fail-soft — a port clash must never take the trading loop down with it.
    control_server = None
    control_task = None
    control_cfg = getattr(settings, "control_api", None)
    if control_cfg is not None and getattr(control_cfg, "enabled", False):
        try:
            import uvicorn

            from .control_api import create_control_app

            port = control_cfg.stocks_port if component == "stocks" else control_cfg.crypto_port
            app = create_control_app(
                storage=storage,
                agent_name=component,
                settings=settings,
                get_positions=getattr(executor, "get_positions", None),
            )
            control_server = uvicorn.Server(
                uvicorn.Config(app, host=control_cfg.host, port=port, log_level="warning")
            )
            control_task = asyncio.create_task(control_server.serve())
            log.info("control API serving", host=control_cfg.host, port=port)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start control API; continuing without it", error=str(exc))

    await agent.start()
    manager.start()
    log.info(f"{component} agent running; Ctrl+C to stop")
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
        if control_server is not None:
            control_server.should_exit = True
        if control_task is not None:
            try:
                await asyncio.wait_for(control_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                control_task.cancel()
        manager.shutdown()
        await agent.shutdown()
        # Release the data provider / execution adapter (the ccxt client owns an
        # aiohttp session that must be closed explicitly, or it leaks on exit).
        await provider.close()
        await executor.close()
        await storage.close()
        log.info(f"{component} agent shut down cleanly")
