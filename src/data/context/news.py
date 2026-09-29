"""RSS 2.0 / Atom news + filings ingest (§7.18, CHANGE.md §4.4).

Each configured feed is downloaded (byte-capped), parsed with the stdlib XML
parser — any document carrying a DTD (``<!DOCTYPE``/``<!ENTITY``) is refused
outright, so entity-expansion tricks never reach the parser — and reduced to
plain text: tags stripped, entities unescaped, whitespace collapsed, length
capped. Items are matched to the traded symbols:

* a feed with ``symbols`` pins every item to them (e.g. an SEC EDGAR per-company
  filings feed);
* otherwise an item matches a symbol when its title/summary names the base asset
  as an upper-case word (``BTC``, ``AAPL``) or any configured alias
  (case-insensitive whole word, e.g. ``bitcoin``).

Unmatched and stale items are dropped. The raw text is stored for the batch
summarizer only — it never reaches a trading prompt.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
import structlog

from ...core.config import NewsFeedSpec
from ...core.models import NewsItem
from .base import ContextBatch, base_asset, get_bytes

logger = structlog.get_logger()

SOURCE = "news"
_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_DTD_RE = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)


def plain_text(raw: str | None, limit: int) -> str:
    """HTML/entity-free, whitespace-collapsed, length-capped text."""
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = html.unescape(_TAG_RE.sub(" ", text))  # descriptions are often double-escaped
    return " ".join(text.split())[:limit]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)  # RFC 822 (RSS pubDate)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value)  # RFC 3339 (Atom)
        except ValueError:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(element: ET.Element | None) -> str | None:
    return element.text if element is not None else None


def parse_feed(body: bytes) -> list[tuple[str, str, str, datetime | None]]:
    """``(title, link, summary, published)`` for every RSS item / Atom entry."""
    if _DTD_RE.search(body):
        raise ValueError("feed carries a DTD — refused")
    root = ET.fromstring(body)  # DTDs refused above; expat >= 2.4.1 besides
    entries: list[tuple[str, str, str, datetime | None]] = []
    for item in root.iter("item"):  # RSS 2.0
        entries.append(
            (
                _text(item.find("title")) or "",
                (_text(item.find("link")) or "").strip(),
                _text(item.find("description")) or "",
                _parse_time(_text(item.find("pubDate"))),
            )
        )
    for entry in root.iter(f"{_ATOM}entry"):  # Atom
        link = ""
        for candidate in entry.findall(f"{_ATOM}link"):
            if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
                link = candidate.get("href", "")
                break
        summary = _text(entry.find(f"{_ATOM}summary")) or _text(entry.find(f"{_ATOM}content"))
        published = _text(entry.find(f"{_ATOM}published")) or _text(entry.find(f"{_ATOM}updated"))
        entries.append(
            (
                _text(entry.find(f"{_ATOM}title")) or "",
                link.strip(),
                summary or "",
                _parse_time(published),
            )
        )
    return entries


class SymbolMatcher:
    """Deterministic item → symbols matching (no model involved)."""

    def __init__(self, symbols: list[str], aliases: dict[str, list[str]]) -> None:
        self._patterns: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = []
        for symbol in symbols:
            ticker = re.compile(rf"(?<![A-Za-z0-9]){re.escape(base_asset(symbol))}(?![A-Za-z0-9])")
            words = aliases.get(symbol) or []
            alias = (
                re.compile(
                    r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in words) + r")(?![a-z0-9])",
                    re.IGNORECASE,
                )
                if words
                else None
            )
            self._patterns.append((symbol, ticker, alias))

    def match(self, text: str) -> list[str]:
        return [
            symbol
            for symbol, ticker, alias in self._patterns
            if ticker.search(text) or (alias is not None and alias.search(text))
        ]


class RssNewsProvider:
    name = SOURCE

    def __init__(
        self,
        client: httpx.AsyncClient,
        feeds: list[NewsFeedSpec],
        aliases: dict[str, list[str]],
        *,
        max_items_per_feed: int,
        max_item_chars: int,
        max_age_hours: float,
        max_feed_bytes: int,
        keep_unmatched: bool = False,
    ) -> None:
        self._client = client
        # §7.83: keep items naming no traded symbol too (``symbols=[]``) — the
        # watchlist counts mentions of *candidates* over them.
        self._keep_unmatched = keep_unmatched
        self._feeds = feeds
        self._aliases = aliases
        self._max_items = max_items_per_feed
        self._max_chars = max_item_chars
        self._max_age = timedelta(hours=max_age_hours)
        self._max_bytes = max_feed_bytes

    async def fetch(self, symbols: list[str], now: datetime) -> ContextBatch:
        matcher = SymbolMatcher(symbols, self._aliases)
        traded = set(symbols)
        items: list[NewsItem] = []
        failures = 0
        for feed in self._feeds:
            try:
                body = await get_bytes(self._client, feed.url, self._max_bytes)
                entries = parse_feed(body)
            except Exception as exc:  # noqa: BLE001 - one dead feed never sinks the pass
                failures += 1
                logger.warning("news feed failed", feed=feed.name, error=str(exc))
                continue
            for title_raw, link, summary_raw, published in entries[: self._max_items]:
                title = plain_text(title_raw, 300)
                if not title or not link.startswith(("https://", "http://")) or len(link) > 1000:
                    continue
                published = published or now
                if published < now - self._max_age or published > now + timedelta(hours=1):
                    continue
                text = plain_text(summary_raw, self._max_chars)
                if feed.symbols:
                    matched = [s for s in feed.symbols if s in traded]
                else:
                    matched = matcher.match(f"{title}\n{text}")
                if not matched and not self._keep_unmatched:
                    continue
                items.append(
                    NewsItem(
                        source=feed.name,
                        url=link,
                        title=title,
                        text=text,
                        published_at=published,
                        symbols=matched,
                    )
                )
        if self._feeds and failures == len(self._feeds):
            raise ValueError("every news feed failed")
        return ContextBatch(source=SOURCE, news=items)
