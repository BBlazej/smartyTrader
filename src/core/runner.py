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
from typing import Any, TextIO

import structlog

logger = structlog.get_logger()

from ..analysis.candles import timeframe_delta
from ..monitoring.alerts import AlertManager, AlertSink, NoopAlertSink, WebhookAlertSink
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


class RunnerAlreadyRunning(RuntimeError):
    """Raised when another runner instance of the same agent owns the lock (§7.52)."""


class RunnerLock:
    """Exclusive per-agent-runner file lock (§7.52).

    Nothing else prevents two runners of the same agent: they would share one SQLite
    DB while keeping *separate* in-memory paper books, exit levels and pending-order
    state — forking decisions and halving every risk guard. An OS ``flock`` is the
    foolproof form: it is released by the kernel even when the holder dies abruptly,
    so there are no stale-pid files to reason about (the pid written inside is
    diagnostics only).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """Take the exclusive lock without blocking; ``False`` when another holds it."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX dev machines only
            logger.warning(
                "fcntl unavailable; running WITHOUT the single-instance runner lock",
                lock_file=str(self._path),
            )
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        """Explicit unlock for graceful paths; an abrupt exit needs none (kernel releases)."""
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001, S110 - closing the fd below releases regardless
            pass
        finally:
            self._handle.close()
            self._handle = None


def build_alerts(settings: Settings) -> AlertManager:
    """Build the alert manager: the log sink always, plus a webhook when configured.

    The webhook (§7.51) is wired only when the ``ALERT_WEBHOOK_URL`` environment
    variable is set — the URL carries tokens, so it never lives in the YAML.
    """
    monitoring = settings.monitoring
    sinks: list[AlertSink] = [NoopAlertSink()]
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if url:
        sinks.append(
            WebhookAlertSink(
                url,
                fmt=getattr(monitoring, "alert_webhook_format", "json"),
                min_severity=getattr(monitoring, "alert_min_severity", "warning"),
            )
        )
    return AlertManager(sinks, dedup_window=float(monitoring.alert_dedup_window_seconds))


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
    timeframe: str | None = None,
    decide_on_new_bar_only: bool = False,
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

    # Single-instance guard (§7.52): refuse a second runner of this agent *before*
    # touching the DB — two processes over one shared SQLite file would trade from
    # separate in-memory books while both stamping the same control row. The lock is
    # held for the whole run; any exit path below (or process death) releases it.
    # An in-memory DB cannot be shared across processes at all, so there is nothing
    # to guard (and no directory to put a lock file in).
    runner_lock: RunnerLock | None = None
    if settings.storage.database_path != ":memory:":
        runner_lock = RunnerLock(
            Path(settings.storage.database_path).parent / f"{component}.runner.lock"
        )
    if runner_lock is not None and not runner_lock.acquire():
        message = (
            f"another {component} runner already holds {runner_lock.path}; refusing to start "
            "a second instance (it would share the DB with separate in-memory state)"
        )
        log.error(message, lock_file=str(runner_lock.path))
        raise RunnerAlreadyRunning(message)

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
        decide_on_new_bar_only=decide_on_new_bar_only,
    )
    agent = build_agent(pipeline, storage, risk_engine, llm_client)

    # Control plane (§7.15): the agent re-reads its ``agent_control`` row each cycle;
    # stored safe-config overrides land on *these* live objects via the closure below.
    # §7.50: an ``interval_minutes`` change also re-arms the live scheduler job —
    # writing it into settings alone was a silent no-op before.
    scheduler_manager = None  # created below on the scheduled path; closure reads late

    def _apply_overrides(raw: str | None) -> None:
        changed = parse_and_apply(settings, component, raw, pipeline=pipeline, agent=agent)
        if "interval_minutes" in changed and scheduler_manager is not None:
            live_settings = getattr(settings, f"{component}_agent", None)
            new_interval = getattr(live_settings, "interval_minutes", None)
            if new_interval:
                scheduler_manager.reschedule_cycle(int(new_interval), job_id=job_id)

    if hasattr(agent, "set_control_overrides_applier"):
        agent.set_control_overrides_applier(_apply_overrides)

    # §7.50: apply stored overrides *before* scheduling so a persisted
    # ``interval_minutes`` (or pairs/market-hours override) governs this run from its
    # first scheduled tick — not only after some later cycle. Fail-soft.
    try:
        control_row = await storage.get_agent_control(component)
        stored_raw = getattr(control_row, "config_override_json", None) if control_row else None
        if stored_raw:
            _apply_overrides(stored_raw)
    except Exception as exc:  # noqa: BLE001 - a broken override must not stop startup
        log.warning(
            "stored config overrides not applied at startup; running on YAML defaults",
            error=str(exc),
        )

    effective_interval = interval_minutes
    live_agent_settings = getattr(settings, f"{component}_agent", None)
    if live_agent_settings is not None and getattr(live_agent_settings, "interval_minutes", None):
        effective_interval = int(live_agent_settings.interval_minutes)

    bar = timeframe_delta(timeframe) if timeframe else None
    if (
        bar is not None
        and not decide_on_new_bar_only
        and effective_interval * 60 < bar.total_seconds()
    ):
        # §7.56: the LLM would re-judge the same closed bars many times per bar.
        log.warning(
            "cycle interval is shorter than the candle timeframe and "
            "decide_on_new_bar_only is off — the LLM re-evaluates each bar repeatedly",
            interval_minutes=effective_interval,
            timeframe=timeframe,
            asks_per_bar=round(bar.total_seconds() / (effective_interval * 60), 1),
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
            if runner_lock is not None:
                runner_lock.release()
        return

    # Late import so tests can patch src.core.scheduler.AsyncSchedulerManager.
    from .scheduler import AsyncSchedulerManager, create_async_scheduler

    manager = AsyncSchedulerManager(create_async_scheduler())
    scheduler_manager = manager
    manager.schedule_cycle(agent.run_cycle, effective_interval, job_id=job_id)
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
        if runner_lock is not None:
            runner_lock.release()
        log.info(f"{component} agent shut down cleanly")
