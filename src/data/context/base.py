"""Shared shapes for the market-context providers (§7.18).

A provider turns one external source into typed rows — :class:`MarketEvent`,
:class:`SentimentReading` or :class:`NewsItem` — and nothing else: it never
touches storage, prompts or orders. The :class:`~src.core.research.ContextRefresher`
calls every enabled provider, isolates their failures and persists the batches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

from ...core.models import MarketEvent, NewsItem, SentimentReading


@dataclass
class ContextBatch:
    """What one provider fetched in one pass.

    ``sync_window`` marks a calendar feed that re-publishes a whole window each time
    (``(start, end)``, ``end=None`` = open-ended): stored events of ``source`` in that
    window that are no longer in ``events`` are deleted — a moved or cancelled event
    must stop blocking entries at its old time. Without it events are only inserted.
    """

    source: str
    events: list[MarketEvent] = field(default_factory=list)
    sync_window: tuple[datetime, datetime | None] | None = None
    sentiment: list[SentimentReading] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)


class ContextProvider(Protocol):
    """One external context source."""

    name: str

    async def fetch(self, symbols: list[str], now: datetime) -> ContextBatch: ...


class FeedTooLarge(ValueError):
    """A download exceeded its byte cap and was abandoned."""


async def get_bytes(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    """GET ``url`` and return the body, aborting once it exceeds ``max_bytes``."""
    chunks: list[bytes] = []
    size = 0
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise FeedTooLarge(f"{url}: response larger than {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def base_asset(symbol: str) -> str:
    """``BTC/EUR`` → ``BTC``; a stock ticker is its own asset (``AAPL`` → ``AAPL``)."""
    return symbol.split("/")[0].strip().upper()


_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9 /%.,()&+:'\-]")
_TAG_RE = re.compile(r"<[^>]*>")


def safe_label(text: str, limit: int = 120) -> str:
    """Reduce external short text (an event title) to a plain, bounded label.

    Calendar titles come from third parties; they reach the trading prompt, so only
    a conservative character set survives and length is capped.
    """
    cleaned = _SAFE_LABEL_RE.sub(" ", _TAG_RE.sub(" ", text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].rstrip()
