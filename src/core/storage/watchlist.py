"""Watchlist persistence for the screener-driven dynamic universe (§7.70).

Entries are agent-scoped through the same ``_agent_scope`` pattern as the trade
tables (§7.39): one agent's dynamic symbols never leak into the other's book.
Rows are self-expiring — :meth:`WatchlistMixin.delete_expired_watchlist_entries`
runs on every refresh, so no separate retention rule is needed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import delete, select

from ..timeutil import to_naive_utc
from .models import WatchlistEntryRow


class WatchlistMixin:
    """``watchlist_entries`` reads/writes (added by :class:`src.core.watchlist`)."""

    async def upsert_watchlist_entry(
        self,
        symbol: str,
        expires_at: datetime,
        *,
        agent: str | None = None,
        source: str = "screener",
        meta: dict[str, object] | None = None,
        added_at: datetime | None = None,
    ) -> WatchlistEntryRow:
        """Add ``symbol`` (or refresh its expiry/meta if the agent already holds it)."""
        scoped_agent = self._agent_scope(agent)
        now = to_naive_utc(added_at or datetime.now(UTC))
        async with await self._session() as session:
            existing = await session.execute(
                select(WatchlistEntryRow).where(
                    WatchlistEntryRow.symbol == symbol,
                    WatchlistEntryRow.agent == scoped_agent,
                )
            )
            row = existing.scalar_one_or_none()
            if row is None:
                row = WatchlistEntryRow(
                    agent=scoped_agent,
                    symbol=symbol,
                    source=source,
                    added_at=now,
                    expires_at=to_naive_utc(expires_at),
                    meta_json=json.dumps(meta or {}),
                )
                session.add(row)
            else:
                row.expires_at = to_naive_utc(expires_at)
                row.meta_json = json.dumps(meta or {})
            await session.commit()
            return row

    async def get_active_watchlist(
        self, agent: str | None = None, now: datetime | None = None
    ) -> list[WatchlistEntryRow]:
        """Unexpired entries for the bound/explicit agent, oldest added first."""
        moment = to_naive_utc(now or datetime.now(UTC))
        async with await self._session() as session:
            query = select(WatchlistEntryRow).where(WatchlistEntryRow.expires_at > moment)
            query = self._where_agent(query, WatchlistEntryRow.agent, agent)
            result = await session.execute(query.order_by(WatchlistEntryRow.added_at))
            return list(result.scalars().all())

    async def delete_expired_watchlist_entries(
        self, agent: str | None = None, now: datetime | None = None
    ) -> int:
        """Drop entries whose TTL has passed; returns the number deleted."""
        moment = to_naive_utc(now or datetime.now(UTC))
        async with await self._session() as session:
            stmt = delete(WatchlistEntryRow).where(WatchlistEntryRow.expires_at <= moment)
            stmt = self._where_agent(stmt, WatchlistEntryRow.agent, agent)
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)
