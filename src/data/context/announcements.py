"""OKX venue announcements — delisting notices (§7.18). Public API, no key.

``GET /api/v5/support/announcements?annType=announcements-delistings`` on the OKX
Europe host lists the latest notices ("OKX to delist DORA, ICX, STORJ, ZEUS and
ELF spot trading pairs"). Only titles that actually announce a delisting count
(the same feed carries "crypto migration" notices); each ticker named in the
title becomes one ``delisting`` event for that asset, dated at publication.
The event guard blocks new entries in a noticed asset — conservative on purpose:
a notice naming ``XYZ/USDT`` also blocks ``XYZ/EUR``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx

from ...core.models import EventImportance, EventKind, MarketEvent
from .base import ContextBatch, safe_label

SOURCE = "okx"
_PATH = "/api/v5/support/announcements"
_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,14}\b")
#: Upper-case words in notice titles that are not assets.
_NOT_ASSETS = frozenset({"OKX", "EEA", "EU", "API", "MICA", "P2P", "EUR", "USD", "AND", "UTC"})


def delisted_assets(title: str) -> list[str]:
    """Tickers a delisting title names (empty when the title is not a delisting)."""
    if "delist" not in title.lower():
        return []
    found = [t for t in _TICKER_RE.findall(title) if t not in _NOT_ASSETS]
    return list(dict.fromkeys(found))


class OkxAnnouncementsProvider:
    name = SOURCE

    def __init__(self, client: httpx.AsyncClient, base_url: str, max_age_days: int) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}{_PATH}"
        self._max_age = timedelta(days=max_age_days)

    async def fetch(self, symbols: list[str], now: datetime) -> ContextBatch:
        response = await self._client.get(self._url, params={"annType": "announcements-delistings"})
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("code")) != "0":
            raise ValueError(
                f"okx announcements error: {payload.get('msg') or payload.get('code')}"
            )
        events: list[MarketEvent] = []
        for page in payload.get("data") or []:
            for notice in page.get("details") or []:
                title = str(notice.get("title", ""))
                try:
                    at = datetime.fromtimestamp(int(notice["pTime"]) / 1000, tz=UTC)
                except (KeyError, TypeError, ValueError):
                    continue
                if now - at > self._max_age:
                    continue
                url = notice.get("url")
                for asset in delisted_assets(title):
                    events.append(
                        MarketEvent(
                            source=SOURCE,
                            kind=EventKind.DELISTING,
                            at=at,
                            title=safe_label(title, 200),
                            asset=asset,
                            importance=EventImportance.HIGH,
                            url=url if isinstance(url, str) and len(url) <= 500 else None,
                        )
                    )
        return ContextBatch(source=SOURCE, events=events)
