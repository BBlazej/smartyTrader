"""Order persistence (storage mixin, §7.36)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from .models import OrderRow


class OrderMixin:
    """``orders`` writes/reads — including the FIFO replay source (§7.25)."""

    async def save_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float | None,
        status: str,
        decision_id: int | None = None,
        filled_at: datetime | None = None,
        agent: str | None = None,
        realized_pnl: float | None = None,
    ) -> int:
        async with await self._session() as session:
            row = OrderRow(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status=status,
                decision_id=decision_id,
                filled_at=filled_at,
                agent=self._agent_scope(agent),
                realized_pnl=realized_pnl,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def update_order_status(
        self,
        order_id: str,
        status: str,
        price: float | None = None,
        filled_at: datetime | None = None,
        realized_pnl: float | None = None,
    ) -> bool:
        """Patch a stored order after venue reconciliation (§7.28).

        Returns ``True`` when a row with ``order_id`` existed. ``price``,
        ``filled_at`` and ``realized_pnl`` are only written when supplied, so a later
        ``canceled`` transition never blanks an earlier fill record.
        """
        values: dict[str, object] = {"status": status}
        if price is not None:
            values["price"] = price
        if filled_at is not None:
            values["filled_at"] = filled_at
        if realized_pnl is not None:
            values["realized_pnl"] = realized_pnl
        async with await self._session() as session:
            result = await session.execute(
                update(OrderRow).where(OrderRow.order_id == order_id).values(**values)
            )
            await session.commit()
            return bool(result.rowcount)

    async def get_recent_orders(
        self,
        symbol: str | None = None,
        limit: int = 20,
        agent: str | None = None,
    ) -> list[OrderRow]:
        async with await self._session() as session:
            stmt = select(OrderRow).order_by(OrderRow.id.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(OrderRow.symbol == symbol)
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(OrderRow.agent == scope)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_recent_closing_fills(
        self, limit: int = 50, agent: str | None = None
    ) -> list[OrderRow]:
        """Newest-first filled orders that realized PnL — one row per closing fill (§7.46).

        The live loss-streak tracker counts exactly these, so restart rehydration
        reads them instead of decision rows (an LLM round trip stamps PnL on both
        the SELL and the entry decision, which double-counted every loss).
        """
        async with await self._session() as session:
            stmt = select(OrderRow).where(
                OrderRow.status == "filled", OrderRow.realized_pnl.isnot(None)
            )
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(OrderRow.agent == scope)
            stmt = stmt.order_by(OrderRow.id.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_filled_decision_ids(self, decision_ids: list[int]) -> set[int]:
        """Which of ``decision_ids`` had an order that filled (§7.45 prompt outcomes).

        Decision ids are globally unique, so no agent scope is needed here.
        """
        if not decision_ids:
            return set()
        async with await self._session() as session:
            stmt = (
                select(OrderRow.decision_id)
                .where(OrderRow.status == "filled", OrderRow.decision_id.in_(decision_ids))
                .distinct()
            )
            result = await session.execute(stmt)
            return {row for row in result.scalars().all() if row is not None}

    async def get_filled_orders(
        self, symbol: str | None = None, agent: str | None = None
    ) -> list[OrderRow]:
        """All *filled* orders in chronological order (insertion order).

        Used at startup to replay the FIFO lot ledger into executors (§7.25):
        rows carry ``side``, ``quantity``, ``price`` and the originating
        ``decision_id``. Ids are monotonic with execution time for both paper
        and venue paths (rows are written when the fill happens). Agent-scoped
        (§7.39): a runner replays only its own fills.
        """
        async with await self._session() as session:
            stmt = select(OrderRow).where(OrderRow.status == "filled").order_by(OrderRow.id.asc())
            if symbol:
                stmt = stmt.where(OrderRow.symbol == symbol)
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(OrderRow.agent == scope)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_pending_orders(self, agent: str | None = None) -> list[OrderRow]:
        """Orders still stored as ``pending``, oldest first (§7.58).

        Reloaded into a venue executor's reconciliation set at startup — an order
        left open across a restart would otherwise stay ``pending`` forever.
        Agent-scoped (§7.39).
        """
        async with await self._session() as session:
            stmt = select(OrderRow).where(OrderRow.status == "pending").order_by(OrderRow.id.asc())
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(OrderRow.agent == scope)
            result = await session.execute(stmt)
            return list(result.scalars().all())
