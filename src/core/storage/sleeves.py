"""Strategy-sleeve persistence (§7.71): allocations, sleeve snapshots, sleeve PnL.

Everything here is agent-scoped (``_agent_scope``, §7.39) and venue-scoped to the
bound executor venue (§7.61): a paper book's sleeves never measure themselves
against a sandbox account's allocation or fills.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from datetime import time as dtime

from sqlalchemy import ColumnElement, Select, func, select

from .models import OrderRow, SleeveSnapshotRow, StrategyAllocationRow, _as_naive_utc


class SleeveMixin:
    """``strategy_allocations`` / ``sleeve_snapshots`` writes + reads."""

    def _venue_eq(self, column: ColumnElement) -> ColumnElement:
        """Exactly the bound venue (``NULL`` when unbound) — these tables are new, no legacy."""
        return column == self._venue if self._venue is not None else column.is_(None)

    def _scoped(self, stmt: Select, table: type, agent: str | None) -> Select:
        scope = self._agent_scope(agent)
        if scope is not None:
            stmt = stmt.where(table.agent == scope)
        return stmt.where(self._venue_eq(table.venue))

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
