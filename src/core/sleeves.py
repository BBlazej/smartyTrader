"""Strategy sleeves (§7.71, CHANGE.md §4.1 / P1).

A **sleeve** is a named trading style — its own candle timeframe, prompt playbook,
holding limit and risk profile — run as one more :class:`DecisionPipeline` inside
the existing agent process (one runner per agent stays true, §7.52). Sleeves share
the provider, LLM client, executor and storage; what separates them is the
``strategy`` tag on every decision and order.

**Position ownership is derived, never stored.** Everything below the pipeline
(paper book, ccxt spot ledger, exit levels) is keyed per symbol, and venues net spot
balances per asset — so v1 enforces a **symbol lock**: a symbol is held by at most
one sleeve at a time. Which sleeve owns an open position follows from the executor's
FIFO ledger: each open lot carries its entry ``decision_id`` (§7.8), and that
decision row carries the sleeve (``llm_decisions.strategy``). The ledger already
survives restarts (§7.25 paper replay, §7.58 venue replay), so ownership and the
time-stop clock do too, with no extra state to keep in sync. Lots without a known
sleeve (pre-§7.71 history, synthetic restart lots, renamed sleeves) belong to the
**first configured sleeve** — deterministic, and never an orphan nobody manages.

**Per-sleeve books and risk (CHANGE.md §4.8).** Each sleeve gets a fixed share of the
agent's capital (``weight × base_equity`` of the latest ``strategy_allocations`` row,
written when the configured weights change). Its equity is that capital plus its own
realized PnL since the allocation plus the unrealized PnL of the positions it owns;
its *book* for the prompt, sizing and the risk gate is ``PortfolioState(cash=equity −
owned market value, positions=owned)``. Every sleeve has its own :class:`RiskEngine`
(the agent ``risk:`` block + the sleeve's ``risk:`` overrides) with its own daily
baseline, drawdown peak and loss streak, rehydrated from ``sleeve_snapshots`` /
tagged closing fills. On top sits one loose agent-wide **backstop** drawdown breaker.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from .config import RiskSettings, SleeveSpec, SleevesSettings
from .control_config import _TIGHTER
from .models import PortfolioState, Position, PositionSide, RiskResult, RiskVerdict
from .portfolio import read_portfolio
from .rehydration import rehydrate_loss_streak
from .risk_engine import RiskEngine

if TYPE_CHECKING:
    from .decision_pipeline import DecisionPipeline

logger = structlog.get_logger()

#: ``PipelineResult.exit_reason`` of a time-stop close (next to stop_loss/take_profit).
TIME_STOP = "time_stop"


def sleeve_risk_settings(
    baseline: RiskSettings, live: RiskSettings, overrides: dict[str, Any]
) -> RiskSettings:
    """A sleeve's effective risk limits (§7.71, CHANGE.md §4.8).

    The agent's YAML ``risk:`` block (``baseline``) is the default, the sleeve's own
    ``risk:`` overrides replace fields of it, and any *tightening* an operator applied
    agent-wide through the safe-config surface (``live`` differs from ``baseline``,
    §7.43) acts as a ceiling on every sleeve — an override can only ever tighten.
    """
    values = {name: getattr(baseline, name) for name in vars(RiskSettings())}
    values.update(overrides)
    for name, direction in _TIGHTER.items():
        live_value, base_value = getattr(live, name), getattr(baseline, name)
        if live_value != base_value:
            values[name] = (
                min(values[name], live_value)
                if direction == "le"
                else max(values[name], live_value)
            )
    return RiskSettings(**values)


@dataclass(frozen=True)
class Allocation:
    """The latest capital split across sleeves (a ``strategy_allocations`` row)."""

    base_equity: float
    weights: dict[str, float]
    created_at: datetime


@dataclass(frozen=True)
class SleeveEquity:
    """One sleeve's book right now (what the risk gate and the snapshots see)."""

    strategy: str
    portfolio: PortfolioState  # cash = equity − owned market value; owned positions
    realized_pnl: float  # since the allocation
    unrealized_pnl: float

    @property
    def equity(self) -> float:
        return self.portfolio.total_value


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


@dataclass(frozen=True)
class Ownership:
    """Which sleeve owns an open position, and since when."""

    strategy: str  # always a configured sleeve name
    # Decision time of the oldest open lot — starts the time stop. ``None`` when no
    # lot has a known decision (synthetic/legacy lots): no time stop is guessed.
    opened_at: datetime | None = None


@dataclass(frozen=True)
class SleeveRun:
    """One sleeve's decision loop inside the agent: its pipeline and candle timeframe."""

    name: str
    pipeline: DecisionPipeline
    timeframe: str


class SleeveBook:
    """Resolves position ownership and time stops for one agent's sleeves.

    Decision metadata (sleeve + timestamp) never changes once written, so lookups
    are cached for the process lifetime — one storage read per new entry decision.
    """

    def __init__(
        self,
        settings: SleevesSettings,
        storage: Any,
        engines: dict[str, RiskEngine] | None = None,
        backstop_engine: RiskEngine | None = None,
    ) -> None:
        if not settings.strategies:
            raise ValueError("SleeveBook needs at least one configured sleeve")
        self._settings = settings
        self._storage = storage
        self._decision_meta: dict[int, tuple[str | None, datetime | None]] = {}
        # Per-sleeve risk engines (own limits/trackers) and the agent-level engine
        # whose seeded peak drives the backstop. Without engines (step-1 wiring,
        # tests) sleeves gate on the pipeline's engine against the whole book.
        self._engines: dict[str, RiskEngine] = dict(engines or {})
        self._backstop_engine = backstop_engine
        self.allocation: Allocation | None = None

    @property
    def per_sleeve_books(self) -> bool:
        """True once sleeves are measured on their own capital (allocation loaded)."""
        return self.allocation is not None

    def engine(self, name: str | None) -> RiskEngine | None:
        """The owning sleeve's risk engine (unknown names → the default sleeve's)."""
        return self._engines.get(self.spec(name).name)

    @property
    def default(self) -> str:
        """The sleeve that owns positions of unknown origin (first configured)."""
        return self._settings.strategies[0].name

    @property
    def names(self) -> list[str]:
        return [spec.name for spec in self._settings.strategies]

    def spec(self, name: str | None) -> SleeveSpec:
        """The sleeve's config; unknown names resolve to the default sleeve."""
        return self._settings.get(name) or self._settings.strategies[0]

    async def ownership(
        self, executor: Any, positions: Iterable[Position], symbol: str
    ) -> Ownership | None:
        """Owner of the open position in ``symbol`` (``None`` when flat).

        A flat symbol with a *working* venue BUY (not filled yet, so not in the
        ledger) is claimed by the sleeve that placed it (§7.72) — otherwise another
        sleeve could enter the same symbol before the fill lands.
        """
        if not any(p.symbol == symbol and p.quantity > 0 for p in positions):
            pending_hook = getattr(executor, "pending_entry_decision_ids", None)
            pending = [i for i in (pending_hook(symbol) if callable(pending_hook) else [])]
            if not pending:
                return None
            await self._load_meta([i for i in pending if i is not None])
            names = [self._decision_meta.get(i, (None, None))[0] for i in pending if i is not None]
            known = next((n for n in names if self._settings.get(n) is not None), None)
            return Ownership(strategy=known or self.default, opened_at=None)
        hook = getattr(executor, "entry_decision_ids", None)
        ids = [i for i in (hook(symbol) if callable(hook) else []) if i is not None]
        await self._load_meta(ids)
        strategy: str | None = None
        opened_at: datetime | None = None
        for decision_id in ids:  # oldest lot first
            name, ts = self._decision_meta.get(decision_id, (None, None))
            if opened_at is None and ts is not None:
                opened_at = _aware(ts)
            if strategy is None and self._settings.get(name) is not None:
                strategy = name
        return Ownership(strategy=strategy or self.default, opened_at=opened_at)

    async def owners(
        self, executor: Any, positions: list[Position] | None = None
    ) -> dict[str, str]:
        """``symbol → owning sleeve`` for every open position."""
        if positions is None:
            positions = await executor.get_positions()
        positions = [p for p in positions if p.quantity > 0]
        owners: dict[str, str] = {}
        for symbol in dict.fromkeys(p.symbol for p in positions):
            ownership = await self.ownership(executor, positions, symbol)
            if ownership is not None:
                owners[symbol] = ownership.strategy
        return owners

    async def strategy_of_decision(self, decision_id: int | None) -> str:
        """The sleeve that made ``decision_id`` (unknown → the default sleeve)."""
        if decision_id is not None:
            await self._load_meta([decision_id])
            name = self._decision_meta.get(decision_id, (None, None))[0]
            if self._settings.get(name) is not None:
                return str(name)
        return self.default

    def time_stop_due(self, ownership: Ownership, now: datetime) -> bool:
        """Has the owning sleeve's holding limit elapsed for this position?"""
        max_hours = self.spec(ownership.strategy).max_holding_hours
        if max_hours is None or ownership.opened_at is None:
            return False
        return _aware(now) - ownership.opened_at >= timedelta(hours=max_hours)

    def held_hours(self, ownership: Ownership, now: datetime) -> float | None:
        """How long the position has been held (``None`` when its start is unknown)."""
        if ownership.opened_at is None:
            return None
        return max(0.0, (_aware(now) - ownership.opened_at).total_seconds() / 3600.0)

    async def _load_meta(self, decision_ids: list[int]) -> None:
        missing = [i for i in dict.fromkeys(decision_ids) if i not in self._decision_meta]
        if not missing:
            return
        found = await self._storage.get_decision_strategies(missing)
        for decision_id in missing:
            # Unknown ids are cached as such — never re-queried every cycle.
            self._decision_meta[decision_id] = found.get(decision_id, (None, None))

    # ── Per-sleeve books (CHANGE.md §4.8) ─────────────────────

    async def ensure_allocation(self, agent_portfolio: PortfolioState) -> Allocation:
        """Load the latest allocation, recording a new one if the weights changed.

        The base is the agent's **cost-basis** equity (cash + open positions at their
        entry price): sleeve equity then adds realized PnL since the allocation plus
        unrealized PnL of owned positions, so the sleeves add up to the agent's equity
        without double-counting gains that were open at allocation time.
        """
        weights = {spec.name: float(spec.weight or 0.0) for spec in self._settings.strategies}
        row = await self._storage.get_latest_allocation()
        stored = json.loads(row.weights_json) if row is not None else None
        if row is None or stored != weights:
            # Cash committed to a still-unbooked BUY is equity too (§7.79).
            base = (
                agent_portfolio.cash
                + agent_portfolio.pending_value
                + sum(
                    p.quantity * p.avg_entry_price * (-1.0 if p.side == PositionSide.SHORT else 1.0)
                    for p in agent_portfolio.positions
                )
            )
            reason = "initial" if row is None else "weights_changed"
            row = await self._storage.record_allocation(base, weights, reason=reason)
            logger.info("sleeve allocation recorded", reason=reason, base_equity=base, **weights)
        self.allocation = Allocation(
            base_equity=float(row.base_equity),
            weights=json.loads(row.weights_json),
            created_at=_aware(row.created_at),
        )
        return self.allocation

    async def sleeve_equity(
        self,
        name: str,
        agent_portfolio: PortfolioState,
        executor: Any,
        owners: dict[str, str] | None = None,
    ) -> SleeveEquity:
        """This sleeve's book: allocated capital + realized since + unrealized of owned."""
        if self.allocation is None:
            raise RuntimeError("sleeve allocation not loaded (call ensure_allocation first)")
        if owners is None:
            owners = await self.owners(executor, agent_portfolio.positions)
        owned = [
            p for p in agent_portfolio.positions if p.quantity > 0 and owners.get(p.symbol) == name
        ]
        realized = await self._storage.get_strategy_realized_pnl(name, self.allocation.created_at)
        unrealized = sum(p.pnl for p in owned)
        equity = (
            self.allocation.weights.get(name, 0.0) * self.allocation.base_equity
            + realized
            + unrealized
        )
        market_value = sum(
            p.quantity * p.current_price * (-1.0 if p.side == PositionSide.SHORT else 1.0)
            for p in owned
        )
        return SleeveEquity(
            strategy=name,
            portfolio=PortfolioState(cash=equity - market_value, positions=owned),
            realized_pnl=realized,
            unrealized_pnl=unrealized,
        )

    def check_backstop(self, agent_total_value: float) -> RiskResult:
        """The loose agent-wide drawdown breaker (approved when no engine is wired)."""
        if self._backstop_engine is None:
            return RiskResult(verdict=RiskVerdict.APPROVED)
        return self._backstop_engine.check_backstop(
            agent_total_value, self._settings.backstop_max_drawdown_pct
        )

    async def record_snapshots(self, executor: Any) -> list[SleeveEquity]:
        """End-of-cycle: persist each sleeve's equity and feed its daily/peak trackers."""
        if self.allocation is None:
            return []
        portfolio = await read_portfolio(executor)
        positions = portfolio.positions
        owners = await self.owners(executor, positions)
        books: list[SleeveEquity] = []
        for name in self.names:
            book = await self.sleeve_equity(name, portfolio, executor, owners)
            engine = self._engines.get(name)
            if engine is not None:
                engine.update_daily_value(book.equity)
            await self._storage.save_sleeve_snapshot(
                strategy=name,
                equity=book.equity,
                cash=book.portfolio.cash,
                positions_value=book.equity - book.portfolio.cash,
                realized_pnl=book.realized_pnl,
                unrealized_pnl=book.unrealized_pnl,
                open_positions=len(book.portfolio.positions),
            )
            books.append(book)
        return books

    async def restore(self) -> None:
        """Rehydrate every sleeve engine at startup (§7.7 semantics, per sleeve).

        Drawdown peak ← MAX(sleeve equity) since the latest allocation (a new
        allocation re-bases every sleeve) or since an operator's audited per-sleeve
        re-baseline (``rebaseline_drawdown.py --strategy``); daily baseline ← today's
        first sleeve snapshot; loss streak ← the sleeve's tagged closing fills. Fail-soft per piece.
        """
        since = self.allocation.created_at if self.allocation is not None else None
        for name, engine in self._engines.items():
            if since is not None:
                try:
                    engine.seed_peak_equity(
                        await self._storage.get_effective_sleeve_peak(name, since)
                    )
                    first = await self._storage.get_first_sleeve_snapshot_of_day(name, since)
                    if first is not None:
                        engine.restore_daily_baseline(float(first.equity))
                except Exception as exc:  # noqa: BLE001 - fresh trackers beat no agent
                    logger.warning(
                        "sleeve tracker rehydration failed", strategy=name, error=str(exc)
                    )
            await rehydrate_loss_streak(engine, self._storage, strategy=name)

    def apply_risk(self, baseline: RiskSettings, live: RiskSettings) -> None:
        """Re-derive every sleeve engine's limits (in place) after an override change."""
        for name, engine in self._engines.items():
            fresh = sleeve_risk_settings(baseline, live, self.spec(name).risk_overrides)
            for field, value in vars(fresh).items():
                setattr(engine.settings, field, value)
