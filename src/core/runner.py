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
from ..analysis.prompt_builder import system_prompt_for
from ..monitoring.alerts import AlertManager, AlertSink, NoopAlertSink, WebhookAlertSink
from .config import Settings
from .control_config import parse_and_apply, risk_baseline
from .decision_pipeline import DecisionPipeline
from .llm_client import LLMClient
from .models import PortfolioState
from .rehydration import executor_venue, rehydrate_from_storage
from .retention import prune_storage
from .risk_engine import RiskEngine
from .sleeves import SleeveBook, SleeveRun, sleeve_risk_settings
from .storage import Storage
from .watchlist import WatchlistManager


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
    # restart cannot reset the guard (§7.5). The read is reset-aware (§7.53): an
    # operator's CLI re-baseline cuts the latch history off at ``reset_at``.
    # Fail-soft: without history the engine seeds lazily from the first reading.
    try:
        risk_engine.seed_peak_equity(await storage.get_effective_peak_equity())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not seed drawdown peak from storage; starting fresh", error=str(exc))

    provider, executor = build_components()
    # Tag this run's order/portfolio rows with the executor's venue (§7.61), so a
    # later paper <-> venue (or sandbox -> live) switch never replays foreign history.
    storage.bind_venue(executor_venue(executor))

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

    # Strategy sleeves (§7.71, opt-in): one more pipeline per sleeve over the same
    # provider/LLM/executor/storage — its own timeframe, playbook prompt and
    # ``strategy`` tag; a shared SleeveBook enforces the symbol lock + time stops.
    # Each sleeve gates on its own book (weight × allocation base + its PnL) with its
    # own RiskEngine; the agent engine keeps the high-water mark for the loose
    # agent-wide backstop. Off → the agent runs ``pipeline`` alone, exactly as before.
    sleeve_runs: list[SleeveRun] = []
    sleeve_book: SleeveBook | None = None
    sleeves_cfg = getattr(getattr(settings, f"{component}_agent", None), "sleeves", None)
    if getattr(sleeves_cfg, "enabled", False) is True and hasattr(agent, "set_sleeves"):
        sleeve_engines = {
            spec.name: RiskEngine(
                sleeve_risk_settings(risk_baseline(settings), settings.risk, spec.risk_overrides)
            )
            for spec in sleeves_cfg.strategies
        }
        sleeve_book = SleeveBook(
            sleeves_cfg, storage, engines=sleeve_engines, backstop_engine=risk_engine
        )
        # Not fail-soft on purpose: without an allocation every sleeve would gate on
        # the whole agent book with its (possibly looser) own limits.
        await sleeve_book.ensure_allocation(
            PortfolioState(cash=await executor.get_cash(), positions=await executor.get_positions())
        )
        await sleeve_book.restore()
        for spec in sleeves_cfg.strategies:
            sleeve_pipeline = DecisionPipeline(
                provider=provider,
                llm_client=llm_client,
                risk_engine=sleeve_engines[spec.name],
                executor=executor,
                system_prompt=system_prompt_for(spec.playbook),
                storage=storage,
                decision_history_limit=decision_history_limit,
                decide_on_new_bar_only=decide_on_new_bar_only,
                strategy=spec.name,
                sleeve_book=sleeve_book,
            )
            sleeve_runs.append(SleeveRun(spec.name, sleeve_pipeline, spec.timeframe))
        agent.set_sleeves(sleeve_runs, sleeve_book)

    # Control plane (§7.15): the agent re-reads its ``agent_control`` row each cycle;
    # stored safe-config overrides land on *these* live objects via the closure below.
    # §7.50: an ``interval_minutes`` change also re-arms the live scheduler job —
    # writing it into settings alone was a silent no-op before.
    scheduler_manager = None  # created below on the scheduled path; closure reads late
    # §7.70: non-core symbols (dynamic + held) from the last watchlist refresh.
    # ``parse_and_apply`` resets the agent to the core list on *any* override change;
    # re-merging these keeps a held dynamic symbol under marking + exit enforcement.
    watchlist_extras: list[str] = []

    def _core_symbols() -> list[str]:
        """The effective YAML (+ override) symbol list, read from the live settings."""
        live = getattr(settings, f"{component}_agent", None)
        return list(getattr(live, "pairs", None) or getattr(live, "symbols", None) or [])

    def _apply_overrides(raw: str | None) -> None:
        changed = parse_and_apply(settings, component, raw, pipeline=pipeline, agent=agent)
        for run in sleeve_runs:  # §7.71: sleeves share the prompt-history depth
            run.pipeline.decision_history_limit = pipeline.decision_history_limit
        if sleeve_book is not None:  # agent-wide risk tightening caps every sleeve (§7.43)
            sleeve_book.apply_risk(risk_baseline(settings), settings.risk)
        if watchlist_extras and hasattr(agent, "set_symbols"):
            agent.set_symbols(list(dict.fromkeys(_core_symbols() + watchlist_extras)))
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

    # Each sleeve decides on its own timeframe (§7.71); without sleeves, the agent's.
    decision_timeframes = [run.timeframe for run in sleeve_runs] or [timeframe]
    for decision_timeframe in decision_timeframes:
        bar = timeframe_delta(decision_timeframe) if decision_timeframe else None
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
                timeframe=decision_timeframe,
                asks_per_bar=round(bar.total_seconds() / (effective_interval * 60), 1),
            )

    # Screener-driven dynamic watchlist (§7.70, opt-in): with it off nothing here
    # runs and the traded set stays exactly the YAML list (+ overrides). Core
    # symbols are re-read from the *live* settings each pass so safe-config
    # overrides flow through; positions are read fresh because a held symbol must
    # never leave the traded set (its marking + exit-level enforcement lives there).
    watchlist_refresh = None
    watchlist_cfg = getattr(live_agent_settings, "watchlist", None)
    if live_agent_settings is not None and getattr(watchlist_cfg, "enabled", False):
        watchlist_manager = WatchlistManager(
            provider=provider,
            storage=storage,
            config=watchlist_cfg,
            component=component,
            quote_currency=getattr(live_agent_settings, "quote_currency", None),
        )

        async def _refresh_watchlist() -> None:
            try:
                core = _core_symbols()
                get_positions = getattr(executor, "get_positions", None)
                if get_positions is None:
                    raise RuntimeError(
                        "executor has no get_positions — held symbols cannot be protected"
                    )
                held = [position.symbol for position in await get_positions()]
                result = await watchlist_manager.refresh(core, held)
                watchlist_extras[:] = [s for s in result.symbols if s not in core]
                if result.symbols and hasattr(agent, "set_symbols"):
                    agent.set_symbols(result.symbols)
            except Exception as exc:  # noqa: BLE001 - never halts trading (fail-soft job)
                log.warning(
                    "watchlist refresh failed; keeping the current symbol list",
                    error=str(exc),
                )

        watchlist_refresh = _refresh_watchlist
        # Initial pass before the first cycle (--once and scheduled alike) so a
        # fresh run already trades its capped dynamic symbols.
        await _refresh_watchlist()

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
    if watchlist_refresh is not None:
        # §7.70: re-run the screener on its own cadence (hours, not cycles).
        manager.schedule_cycle(
            watchlist_refresh,
            watchlist_cfg.refresh_minutes,
            job_id="watchlist_refresh",
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
