"""Market + portfolio snapshot persistence (storage mixin, §7.36)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

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
    full history seeds the drawdown high-water mark (§7.7).
    """

    async def save_portfolio_snapshot(
        self,
        cash: float,
        positions_json: str,
        total_value: float,
        unrealized_pnl: float = 0.0,
    ) -> int:
        async with await self._session() as session:
            row = PortfolioSnapshotRow(
                cash=cash,
                positions_json=positions_json,
                total_value=total_value,
                unrealized_pnl=unrealized_pnl,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_first_portfolio_snapshot_of_day(self) -> PortfolioSnapshotRow | None:
        """Earliest portfolio snapshot of the current UTC day (or ``None``).

        Its ``total_value`` rehydrates today's daily-loss baseline after a
        restart (§7.7). Timestamps are stored as naive UTC.
        """
        from datetime import time as dtime

        day_start = datetime.combine(datetime.now(UTC).date(), dtime.min)
        async with await self._session() as session:
            stmt = (
                select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.timestamp >= day_start)
                .order_by(PortfolioSnapshotRow.timestamp.asc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_max_portfolio_value(self) -> float | None:
        """Highest total_value ever recorded in portfolio snapshots (or ``None``).

        Used to seed the risk engine's drawdown high-water mark at startup so
        the guard persists across process restarts.
        """
        from sqlalchemy import func

        async with await self._session() as session:
            result = await session.execute(select(func.max(PortfolioSnapshotRow.total_value)))
            value = result.scalar()
            return float(value) if value is not None else None

    async def get_latest_portfolio_snapshot(self) -> PortfolioSnapshotRow | None:
        async with await self._session() as session:
            stmt = select(PortfolioSnapshotRow).order_by(PortfolioSnapshotRow.id.desc()).limit(1)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_portfolio_history(self, limit: int = 100) -> list[PortfolioSnapshotRow]:
        async with await self._session() as session:
            stmt = (
                select(PortfolioSnapshotRow).order_by(PortfolioSnapshotRow.id.desc()).limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
