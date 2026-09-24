"""Market + portfolio snapshot persistence (storage mixin, §7.36)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, select

from .models import MarketSnapshotRow, PortfolioSnapshotRow


class MarketSnapshotMixin:
    """``market_snapshots`` writes/reads — candle cache per symbol-cycle."""

    async def save_market_snapshot(
        self,
        symbol: str,
        timeframe: str,
        candles_json: str,
        indicators_json: str = "{}",
    ) -> int:
        async with await self._session() as session:
            row = MarketSnapshotRow(
                symbol=symbol,
                timeframe=timeframe,
                candles_json=candles_json,
                indicators_json=indicators_json,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_recent_snapshots(
        self,
        symbol: str,
        limit: int = 10,
    ) -> list[MarketSnapshotRow]:
        async with await self._session() as session:
            stmt = (
                select(MarketSnapshotRow)
                .where(MarketSnapshotRow.symbol == symbol)
                .order_by(MarketSnapshotRow.fetched_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())


class PortfolioSnapshotMixin:
    """``portfolio_snapshots`` writes/reads — **never pruned** (drawdown seed).

    The first snapshot of the UTC day rehydrates the daily-loss baseline; MAX over
    full history seeds the drawdown high-water mark (§7.7). Every read is agent-scoped
    (§7.39): each agent has its own book, baseline and peak.
    """

    async def save_portfolio_snapshot(
        self,
        cash: float,
        positions_json: str,
        total_value: float,
        unrealized_pnl: float = 0.0,
        agent: str | None = None,
    ) -> int:
        async with await self._session() as session:
            row = PortfolioSnapshotRow(
                cash=cash,
                positions_json=positions_json,
                total_value=total_value,
                unrealized_pnl=unrealized_pnl,
                agent=self._agent_scope(agent),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_first_portfolio_snapshot_of_day(
        self, agent: str | None = None
    ) -> PortfolioSnapshotRow | None:
        """Earliest portfolio snapshot of the current UTC day (or ``None``).

        Its ``total_value`` rehydrates today's daily-loss baseline after a
        restart (§7.7). Timestamps are stored as naive UTC.
        """
        from datetime import time as dtime

        day_start = datetime.combine(datetime.now(UTC).date(), dtime.min)
        async with await self._session() as session:
            stmt = self._scoped_snapshots(
                select(PortfolioSnapshotRow).where(PortfolioSnapshotRow.timestamp >= day_start),
                agent,
            )
            stmt = stmt.order_by(PortfolioSnapshotRow.timestamp.asc()).limit(1)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_max_portfolio_value(self, agent: str | None = None) -> float | None:
        """Highest total_value ever recorded in portfolio snapshots (or ``None``).

        Used to seed the risk engine's drawdown high-water mark at startup so
        the guard persists across process restarts.
        """
        from sqlalchemy import func

        async with await self._session() as session:
            stmt = self._scoped_snapshots(select(func.max(PortfolioSnapshotRow.total_value)), agent)
            result = await session.execute(stmt)
            value = result.scalar()
            return float(value) if value is not None else None

    async def get_latest_portfolio_snapshot(
        self, agent: str | None = None
    ) -> PortfolioSnapshotRow | None:
        async with await self._session() as session:
            stmt = self._scoped_snapshots(select(PortfolioSnapshotRow), agent)
            stmt = stmt.order_by(PortfolioSnapshotRow.id.desc()).limit(1)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_portfolio_history(
        self, limit: int = 100, agent: str | None = None
    ) -> list[PortfolioSnapshotRow]:
        async with await self._session() as session:
            stmt = self._scoped_snapshots(select(PortfolioSnapshotRow), agent)
            stmt = stmt.order_by(PortfolioSnapshotRow.id.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    def _scoped_snapshots(self, stmt: Select, agent: str | None) -> Select:
        """Restrict a ``portfolio_snapshots`` query to the effective agent (§7.39)."""
        scope = self._agent_scope(agent)
        if scope is not None:
            stmt = stmt.where(PortfolioSnapshotRow.agent == scope)
        return stmt
