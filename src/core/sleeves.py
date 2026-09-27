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
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .config import SleeveSpec, SleevesSettings
from .models import Position

if TYPE_CHECKING:
    from .decision_pipeline import DecisionPipeline

#: ``PipelineResult.exit_reason`` of a time-stop close (next to stop_loss/take_profit).
TIME_STOP = "time_stop"


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

    def __init__(self, settings: SleevesSettings, storage: Any) -> None:
        if not settings.strategies:
            raise ValueError("SleeveBook needs at least one configured sleeve")
        self._settings = settings
        self._storage = storage
        self._decision_meta: dict[int, tuple[str | None, datetime | None]] = {}

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
        """Owner of the open position in ``symbol`` (``None`` when flat)."""
        if not any(p.symbol == symbol and p.quantity > 0 for p in positions):
            return None
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

    async def owners(self, executor: Any) -> dict[str, str]:
        """``symbol → owning sleeve`` for every open position (close-all tagging)."""
        positions = [p for p in await executor.get_positions() if p.quantity > 0]
        owners: dict[str, str] = {}
        for symbol in dict.fromkeys(p.symbol for p in positions):
            ownership = await self.ownership(executor, positions, symbol)
            if ownership is not None:
                owners[symbol] = ownership.strategy
        return owners

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
