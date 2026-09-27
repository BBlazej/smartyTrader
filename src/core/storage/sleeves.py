"""Strategy-sleeve persistence (§7.71): allocations, sleeve snapshots, sleeve PnL.

Everything here is agent-scoped (``_agent_scope``, §7.39) and venue-scoped to the
bound executor venue (§7.61): a paper book's sleeves never measure themselves
against a sandbox account's allocation or fills.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from datetime import time as dtime

import structlog
from sqlalchemy import Select, func, select

from .models import (
    OrderRow,
    SleeveDrawdownResetRow,
    SleeveSnapshotRow,
    StrategyAllocationRow,
    _as_naive_utc,
)

logger = structlog.get_logger()


class SleeveMixin:
    """``strategy_allocations`` / ``sleeve_snapshots`` writes + reads."""

    def _scoped(self, stmt: Select, table: type, agent: str | None) -> Select:
        """Agent scope (§7.39) + exactly the bound venue (§7.61) — these tables are new,
        so there are no legacy NULL rows to admit. An unbound Storage (dashboard, CLIs)
        reads every venue."""
        scope = self._agent_scope(agent)
        if scope is not None:
            stmt = stmt.where(table.agent == scope)
        if self._venue is not None:
            stmt = stmt.where(table.venue == self._venue)
        return stmt

    # ── Allocations ───────────────────────────────────────

    async def record_allocation(
        self,
        base_equity: float,
        weights: dict[str, float],
        reason: str = "initial",
        agent: str | None = None,
    ) -> StrategyAllocationRow:
        async with await self._session() as session:
            row = StrategyAllocationRow(
                agent=self._agent_scope(agent),
                venue=self._venue,
                base_equity=float(base_equity),
                weights_json=json.dumps(weights, sort_keys=True),
                reason=reason,
            )
            session.add(row)
            await session.commit()
            return row

    async def get_latest_allocation(self, agent: str | None = None) -> StrategyAllocationRow | None:
        async with await self._session() as session:
            stmt = self._scoped(select(StrategyAllocationRow), StrategyAllocationRow, agent)
            stmt = stmt.order_by(StrategyAllocationRow.id.desc()).limit(1)
            return (await session.execute(stmt)).scalars().first()

    # ── Sleeve realized PnL ───────────────────────────────

    async def get_strategy_realized_pnl(
        self, strategy: str, since: datetime, agent: str | None = None
    ) -> float:
        """Sum of the sleeve's closing-fill PnL filled at/after ``since`` (0 when none)."""
        async with await self._session() as session:
            stmt = select(func.coalesce(func.sum(OrderRow.realized_pnl), 0.0)).where(
                OrderRow.status == "filled",
                OrderRow.strategy == strategy,
                OrderRow.realized_pnl.isnot(None),
                OrderRow.filled_at >= _as_naive_utc(since),
            )
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(OrderRow.agent == scope)
            if self._venue is not None:
                stmt = stmt.where(self._venue_match(OrderRow.venue, self._venue))
            return float((await session.execute(stmt)).scalar() or 0.0)

    async def get_strategy_orders(
        self, strategy: str, since: datetime | None = None, agent: str | None = None
    ) -> list[OrderRow]:
        """The sleeve's *filled* orders, oldest first (§7.73 performance ledger)."""
        async with await self._session() as session:
            stmt = select(OrderRow).where(
                OrderRow.status == "filled", OrderRow.strategy == strategy
            )
            if since is not None:
                stmt = stmt.where(OrderRow.filled_at >= _as_naive_utc(since))
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(OrderRow.agent == scope)
            if self._venue is not None:
                stmt = stmt.where(self._venue_match(OrderRow.venue, self._venue))
            return list((await session.execute(stmt.order_by(OrderRow.id.asc()))).scalars())

    async def get_sleeve_equity_series(
        self, strategy: str, since: datetime | None = None, agent: str | None = None
    ) -> list[float]:
        """The sleeve's snapshot equity values, oldest first (§7.73 drawdown)."""
        async with await self._session() as session:
            stmt = self._scoped(select(SleeveSnapshotRow.equity), SleeveSnapshotRow, agent).where(
                SleeveSnapshotRow.strategy == strategy
            )
            if since is not None:
                stmt = stmt.where(SleeveSnapshotRow.timestamp >= _as_naive_utc(since))
            stmt = stmt.order_by(SleeveSnapshotRow.timestamp.asc(), SleeveSnapshotRow.id.asc())
            return [float(v) for v in (await session.execute(stmt)).scalars()]

    # ── Sleeve snapshots ──────────────────────────────────

    async def save_sleeve_snapshot(
        self,
        strategy: str,
        equity: float,
        cash: float,
        positions_value: float = 0.0,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        open_positions: int = 0,
        agent: str | None = None,
    ) -> int:
        async with await self._session() as session:
            row = SleeveSnapshotRow(
                agent=self._agent_scope(agent),
                venue=self._venue,
                strategy=strategy,
                equity=equity,
                cash=cash,
                positions_value=positions_value,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                open_positions=open_positions,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_sleeve_peak_equity(
        self, strategy: str, since: datetime, agent: str | None = None
    ) -> float | None:
        """MAX(equity) of the sleeve since ``since`` — its drawdown high-water seed."""
        async with await self._session() as session:
            stmt = self._scoped(
                select(func.max(SleeveSnapshotRow.equity)), SleeveSnapshotRow, agent
            ).where(
                SleeveSnapshotRow.strategy == strategy,
                SleeveSnapshotRow.timestamp >= _as_naive_utc(since),
            )
            value = (await session.execute(stmt)).scalar()
            return float(value) if value is not None else None

    async def get_latest_sleeve_snapshot(
        self, strategy: str, agent: str | None = None
    ) -> SleeveSnapshotRow | None:
        async with await self._session() as session:
            stmt = self._scoped(select(SleeveSnapshotRow), SleeveSnapshotRow, agent).where(
                SleeveSnapshotRow.strategy == strategy
            )
            stmt = stmt.order_by(SleeveSnapshotRow.id.desc()).limit(1)
            return (await session.execute(stmt)).scalars().first()

    # ── Sleeve drawdown re-baseline (§7.71, as §7.53) ─────

    async def record_sleeve_drawdown_reset(
        self, strategy: str, baseline_value: float, agent: str | None = None
    ) -> SleeveDrawdownResetRow:
        """Persist an operator's explicit sleeve re-baseline (audited, CLI-only)."""
        scope = self._agent_scope(agent)
        if scope is None:
            raise ValueError("sleeve drawdown resets require an explicit agent")
        async with await self._session() as session:
            row = await session.get(SleeveDrawdownResetRow, (scope, strategy))
            if row is None:
                row = SleeveDrawdownResetRow(
                    agent=scope, strategy=strategy, baseline_value=baseline_value
                )
                session.add(row)
            else:
                row.baseline_value = baseline_value
                row.reset_at = datetime.now(UTC)
            await session.commit()
            logger.info(
                "sleeve drawdown peak rebaselined",
                agent=scope,
                strategy=strategy,
                baseline_value=baseline_value,
                reset_at=str(row.reset_at),
            )
            return row

    async def get_sleeve_drawdown_reset(
        self, strategy: str, agent: str | None = None
    ) -> SleeveDrawdownResetRow | None:
        scope = self._agent_scope(agent)
        if scope is None:
            return None
        async with await self._session() as session:
            return await session.get(SleeveDrawdownResetRow, (scope, strategy))

    async def get_effective_sleeve_peak(
        self, strategy: str, since: datetime, agent: str | None = None
    ) -> float | None:
        """The sleeve's drawdown high-water seed: MAX(equity) since the allocation, or —
        after an operator re-baseline newer than it — ``max(baseline, MAX since reset)``."""
        reset = await self.get_sleeve_drawdown_reset(strategy, agent)
        if reset is None or _as_naive_utc(reset.reset_at) < _as_naive_utc(since):
            return await self.get_sleeve_peak_equity(strategy, since, agent)
        peak = await self.get_sleeve_peak_equity(strategy, reset.reset_at, agent)
        return (
            max(float(reset.baseline_value), peak)
            if peak is not None
            else float(reset.baseline_value)
        )

    async def get_position_strategies(
        self, symbols: list[str], agent: str | None = None
    ) -> dict[str, str]:
        """``symbol → sleeve`` of each symbol's latest filled BUY (dashboard ownership).

        Under the symbol lock only the owning sleeve buys a held symbol, so its latest
        entry names the owner; the runner itself derives ownership from the ledger.
        """
        if not symbols:
            return {}
        async with await self._session() as session:
            stmt = (
                select(OrderRow.symbol, OrderRow.strategy)
                .where(
                    OrderRow.status == "filled",
                    OrderRow.side == "buy",
                    OrderRow.strategy.isnot(None),
                    OrderRow.symbol.in_(symbols),
                )
                .order_by(OrderRow.id.desc())
            )
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(OrderRow.agent == scope)
            owners: dict[str, str] = {}
            for symbol, strategy in (await session.execute(stmt)).all():
                owners.setdefault(symbol, strategy)
            return owners

    async def get_first_sleeve_snapshot_of_day(
        self, strategy: str, since: datetime | None = None, agent: str | None = None
    ) -> SleeveSnapshotRow | None:
        """The sleeve's earliest snapshot of the current UTC day (at/after ``since``)."""
        start = datetime.combine(datetime.now(UTC).date(), dtime.min)
        if since is not None:
            start = max(start, _as_naive_utc(since))
        async with await self._session() as session:
            stmt = self._scoped(select(SleeveSnapshotRow), SleeveSnapshotRow, agent).where(
                SleeveSnapshotRow.strategy == strategy, SleeveSnapshotRow.timestamp >= start
            )
            stmt = stmt.order_by(SleeveSnapshotRow.timestamp.asc()).limit(1)
            return (await session.execute(stmt)).scalars().first()

    async def get_latest_sleeve_snapshots(
        self, agent: str | None = None
    ) -> list[SleeveSnapshotRow]:
        """Newest snapshot per sleeve (dashboard table), ordered by strategy name."""
        async with await self._session() as session:
            latest = self._scoped(
                select(func.max(SleeveSnapshotRow.id)), SleeveSnapshotRow, agent
            ).group_by(SleeveSnapshotRow.strategy)
            stmt = (
                select(SleeveSnapshotRow)
                .where(SleeveSnapshotRow.id.in_(latest))
                .order_by(SleeveSnapshotRow.strategy)
            )
            return list((await session.execute(stmt)).scalars().all())
