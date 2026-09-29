"""Market-context persistence (§7.18, CHANGE.md §4.4/§4.6).

Four agent-scoped tables (``_agent_scope``, §7.39): ``market_events`` (calendar
data the event guard reads), ``sentiment_readings``, ``news_items`` (raw text for
the summarizer only) and ``context_cards`` (validated summarizer output). Writes
are idempotent — every refresh may re-deliver what an earlier one stored.
Retention: :meth:`ContextMixin.prune_context` (called from ``Storage.prune``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select

from ..models import (
    ContextCard,
    EventImportance,
    EventKind,
    MarketEvent,
    NewsItem,
    SentimentReading,
)
from .models import (
    ContextCardRow,
    MarketEventRow,
    NewsItemRow,
    SentimentReadingRow,
    _as_naive_utc,
)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive UTC; the context models carry aware datetimes."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _event_from_row(row: MarketEventRow) -> MarketEvent:
    return MarketEvent(
        source=row.source,
        kind=EventKind(row.kind),
        at=_aware(row.at),
        title=row.title,
        asset=row.asset,
        currency=row.currency,
        importance=EventImportance(row.importance),
        url=row.url,
    )


def _news_from_row(row: NewsItemRow) -> NewsItem:
    return NewsItem(
        source=row.source,
        url=row.url,
        title=row.title,
        text=row.body,
        published_at=_aware(row.published_at),
        symbols=json.loads(row.symbols_json or "[]"),
    )


class ContextMixin:
    """Reads/writes for the market-context tables."""

    # ── Events ────────────────────────────────────────────

    async def store_market_events(
        self, events: Iterable[MarketEvent], agent: str | None = None
    ) -> int:
        """Insert events not stored yet (by ``dedup_key``); returns how many were new."""
        scoped = self._agent_scope(agent)
        batch = {event.dedup_key(): event for event in events}
        if not batch:
            return 0
        async with await self._session() as session:
            stmt = select(MarketEventRow.dedup_key).where(MarketEventRow.dedup_key.in_(batch))
            if scoped is not None:
                stmt = stmt.where(MarketEventRow.agent == scoped)
            known = set((await session.execute(stmt)).scalars().all())
            new = [(key, event) for key, event in batch.items() if key not in known]
            for key, event in new:
                session.add(
                    MarketEventRow(
                        agent=scoped,
                        source=event.source,
                        kind=event.kind.value,
                        asset=event.asset,
                        currency=event.currency,
                        at=_as_naive_utc(event.at),
                        importance=event.importance.value,
                        title=event.title,
                        url=event.url,
                        dedup_key=key,
                    )
                )
            await session.commit()
            return len(new)

    async def sync_market_events(
        self,
        source: str,
        events: Iterable[MarketEvent],
        start: datetime,
        end: datetime | None = None,
        agent: str | None = None,
    ) -> tuple[int, int]:
        """Make ``source``'s stored events in ``[start, end]`` equal ``events``.

        For calendar feeds that re-publish a whole window each time (the weekly
        macro feed, the YAML list): an event moved or dropped upstream must not
        keep blocking entries at its old time. Rows outside the window (history,
        or weeks the feed no longer covers) are untouched. ``end=None`` = open-ended.
        Returns ``(inserted, deleted)``.
        """
        scoped = self._agent_scope(agent)
        batch = list(events)
        keys = {event.dedup_key() for event in batch}
        async with await self._session() as session:
            stmt = delete(MarketEventRow).where(
                MarketEventRow.source == source,
                MarketEventRow.at >= _as_naive_utc(start),
            )
            if end is not None:
                stmt = stmt.where(MarketEventRow.at <= _as_naive_utc(end))
            if keys:
                stmt = stmt.where(MarketEventRow.dedup_key.not_in(keys))
            if scoped is not None:
                stmt = stmt.where(MarketEventRow.agent == scoped)
            deleted = int((await session.execute(stmt)).rowcount or 0)
            await session.commit()
        inserted = await self.store_market_events(batch, agent=agent)
        return inserted, deleted

    async def get_market_events(
        self,
        start: datetime,
        end: datetime,
        *,
        assets: Iterable[str] | None = None,
        kinds: Iterable[EventKind] | None = None,
        agent: str | None = None,
    ) -> list[MarketEvent]:
        """Events with ``start <= at <= end``, oldest first.

        ``assets`` limits asset-specific events to those assets; market-wide rows
        (``asset`` NULL) are always included. ``kinds`` filters by event kind.
        """
        async with await self._session() as session:
            stmt = select(MarketEventRow).where(
                MarketEventRow.at >= _as_naive_utc(start),
                MarketEventRow.at <= _as_naive_utc(end),
            )
            if assets is not None:
                wanted = [a.upper() for a in assets]
                stmt = stmt.where(
                    or_(MarketEventRow.asset.is_(None), MarketEventRow.asset.in_(wanted))
                )
            if kinds is not None:
                stmt = stmt.where(MarketEventRow.kind.in_([k.value for k in kinds]))
            scoped = self._agent_scope(agent)
            if scoped is not None:
                stmt = stmt.where(MarketEventRow.agent == scoped)
            rows = (await session.execute(stmt.order_by(MarketEventRow.at))).scalars().all()
            return [_event_from_row(row) for row in rows]

    # ── Sentiment ─────────────────────────────────────────

    async def store_sentiment(
        self, readings: Iterable[SentimentReading], agent: str | None = None
    ) -> int:
        """Insert readings not stored yet (one per source × ``as_of``)."""
        scoped = self._agent_scope(agent)
        inserted = 0
        async with await self._session() as session:
            for reading in readings:
                stmt = select(SentimentReadingRow.id).where(
                    SentimentReadingRow.source == reading.source,
                    SentimentReadingRow.as_of == _as_naive_utc(reading.as_of),
                )
                if scoped is not None:
                    stmt = stmt.where(SentimentReadingRow.agent == scoped)
                if (await session.execute(stmt)).first() is not None:
                    continue
                session.add(
                    SentimentReadingRow(
                        agent=scoped,
                        source=reading.source,
                        value=float(reading.value),
                        label=reading.label,
                        as_of=_as_naive_utc(reading.as_of),
                    )
                )
                inserted += 1
            await session.commit()
        return inserted

    async def get_latest_sentiment(
        self, source: str, agent: str | None = None
    ) -> SentimentReading | None:
        async with await self._session() as session:
            stmt = select(SentimentReadingRow).where(SentimentReadingRow.source == source)
            scoped = self._agent_scope(agent)
            if scoped is not None:
                stmt = stmt.where(SentimentReadingRow.agent == scoped)
            row = (
                (await session.execute(stmt.order_by(SentimentReadingRow.as_of.desc()).limit(1)))
                .scalars()
                .first()
            )
            if row is None:
                return None
            return SentimentReading(
                source=row.source, value=row.value, label=row.label, as_of=_aware(row.as_of)
            )

    # ── News ──────────────────────────────────────────────

    async def store_news_items(self, items: Iterable[NewsItem], agent: str | None = None) -> int:
        """Insert items not stored yet (by ``content_hash``); returns how many were new."""
        scoped = self._agent_scope(agent)
        batch = {item.content_hash(): item for item in items}
        if not batch:
            return 0
        async with await self._session() as session:
            stmt = select(NewsItemRow.content_hash).where(NewsItemRow.content_hash.in_(batch))
            if scoped is not None:
                stmt = stmt.where(NewsItemRow.agent == scoped)
            known = set((await session.execute(stmt)).scalars().all())
            new = [(key, item) for key, item in batch.items() if key not in known]
            for key, item in new:
                session.add(
                    NewsItemRow(
                        agent=scoped,
                        source=item.source,
                        url=item.url,
                        title=item.title,
                        body=item.text,
                        published_at=_as_naive_utc(item.published_at),
                        symbols_json=json.dumps(sorted(set(item.symbols))),
                        content_hash=key,
                    )
                )
            await session.commit()
            return len(new)

    async def get_news_for_symbol(
        self, symbol: str, since: datetime, limit: int = 20, agent: str | None = None
    ) -> list[NewsItem]:
        """Items matched to ``symbol`` published after ``since``, newest first."""
        async with await self._session() as session:
            stmt = select(NewsItemRow).where(
                NewsItemRow.published_at > _as_naive_utc(since),
                # symbols_json is a sorted JSON list of strings; the quoted form
                # matches exactly one element (no BTC/EUR ⊂ WBTC/EUR false hit).
                NewsItemRow.symbols_json.contains(json.dumps(symbol)),
            )
            scoped = self._agent_scope(agent)
            if scoped is not None:
                stmt = stmt.where(NewsItemRow.agent == scoped)
            stmt = stmt.order_by(NewsItemRow.published_at.desc()).limit(max(0, limit))
            return [_news_from_row(row) for row in (await session.execute(stmt)).scalars()]

    # ── Context cards ─────────────────────────────────────

    async def store_context_card(
        self,
        card: ContextCard,
        expires_at: datetime,
        *,
        model: str | None = None,
        news_through: datetime | None = None,
        agent: str | None = None,
    ) -> ContextCardRow:
        async with await self._session() as session:
            row = ContextCardRow(
                agent=self._agent_scope(agent),
                symbol=card.symbol,
                card_json=card.model_dump_json(),
                model=model,
                news_through=_as_naive_utc(news_through) if news_through else None,
                expires_at=_as_naive_utc(expires_at),
            )
            session.add(row)
            await session.commit()
            return row

    async def get_latest_context_card_row(
        self, symbol: str, agent: str | None = None
    ) -> ContextCardRow | None:
        """The newest card row for ``symbol``, expired or not (summarizer bookkeeping)."""
        async with await self._session() as session:
            stmt = select(ContextCardRow).where(ContextCardRow.symbol == symbol)
            scoped = self._agent_scope(agent)
            if scoped is not None:
                stmt = stmt.where(ContextCardRow.agent == scoped)
            stmt = stmt.order_by(ContextCardRow.id.desc()).limit(1)
            return (await session.execute(stmt)).scalars().first()

    async def get_active_context_card(
        self, symbol: str, now: datetime | None = None, agent: str | None = None
    ) -> ContextCard | None:
        """The newest *unexpired* card for ``symbol`` (``None`` when none is fresh)."""
        row = await self.get_latest_context_card_row(symbol, agent=agent)
        moment = _as_naive_utc(now or datetime.now(UTC))
        if row is None or row.expires_at <= moment:
            return None
        return ContextCard.model_validate_json(row.card_json)

    # ── Retention ─────────────────────────────────────────

    async def prune_context(self, days: int, now: datetime | None = None) -> dict[str, int]:
        """Delete context rows older than ``days`` (``<= 0`` disables)."""
        if days <= 0:
            return {}
        cutoff = _as_naive_utc(now or datetime.now(UTC)) - timedelta(days=days)
        counts: dict[str, int] = {}
        async with await self._session() as session:
            for name, stmt in (
                ("market_events", delete(MarketEventRow).where(MarketEventRow.at < cutoff)),
                (
                    "sentiment_readings",
                    delete(SentimentReadingRow).where(SentimentReadingRow.as_of < cutoff),
                ),
                ("news_items", delete(NewsItemRow).where(NewsItemRow.published_at < cutoff)),
                (
                    "context_cards",
                    delete(ContextCardRow).where(ContextCardRow.expires_at < cutoff),
                ),
            ):
                counts[name] = int((await session.execute(stmt)).rowcount or 0)
            await session.commit()
        return counts
