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
    ) -> bool:
        """Patch a stored order after venue reconciliation (§7.28).

        Returns ``True`` when a row with ``order_id`` existed. ``price`` and
        ``filled_at`` are only written when supplied, so a later ``canceled``
        transition never blanks an earlier fill record.
        """
        values: dict[str, object] = {"status": status}
        if price is not None:
            values["price"] = price
        if filled_at is not None:
            values["filled_at"] = filled_at
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
    ) -> list[OrderRow]:
        async with await self._session() as session:
            stmt = select(OrderRow).order_by(OrderRow.id.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(OrderRow.symbol == symbol)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_filled_orders(self, symbol: str | None = None) -> list[OrderRow]:
        """All *filled* orders in chronological order (insertion order).

        Used at startup to replay the FIFO lot ledger into executors (§7.25):
        rows carry ``side``, ``quantity``, ``price`` and the originating
        ``decision_id``. Ids are monotonic with execution time for both paper
        and venue paths (rows are written when the fill happens).
        """
        async with await self._session() as session:
            stmt = select(OrderRow).where(OrderRow.status == "filled").order_by(OrderRow.id.asc())
            if symbol:
                stmt = stmt.where(OrderRow.symbol == symbol)
            result = await session.execute(stmt)
            return list(result.scalars().all())
