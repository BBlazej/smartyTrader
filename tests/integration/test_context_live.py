"""Live market-context smoke tests (§7.18) — opt-in, real network.

Prove the three free, key-less endpoints the crypto context uses still answer in
the shape the providers parse: alternative.me Fear & Greed, the ForexFactory weekly
calendar and OKX Europe delisting announcements. Excluded from default runs; on a
connected machine:

    pytest tests/integration/test_context_live.py -m network --no-cov -q
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from src.core.config import AnnouncementsSettings, MacroCalendarSettings, SentimentSettings
from src.core.models import EventKind
from src.data.context.announcements import OkxAnnouncementsProvider
from src.data.context.calendar import ForexFactoryProvider
from src.data.context.sentiment import FearGreedProvider

pytestmark = [pytest.mark.network]


@pytest.fixture()
async def client():
    async with httpx.AsyncClient(
        timeout=20, headers={"User-Agent": "trading-agent/0.1 (smoke test)"}
    ) as c:
        yield c


async def test_fear_greed_live(client: httpx.AsyncClient) -> None:
    batch = await FearGreedProvider(client, SentimentSettings().url).fetch([], datetime.now(UTC))
    assert batch.sentiment and 0 <= batch.sentiment[0].value <= 100


async def test_forexfactory_live(client: httpx.AsyncClient) -> None:
    provider = ForexFactoryProvider(
        client, MacroCalendarSettings().feed_url, ["USD", "EUR"], "high"
    )
    batch = await provider.fetch([], datetime.now(UTC))
    assert batch.sync_window is not None
    assert all(e.kind is EventKind.MACRO for e in batch.events)


async def test_okx_announcements_live(client: httpx.AsyncClient) -> None:
    provider = OkxAnnouncementsProvider(client, AnnouncementsSettings().base_url, 365)
    batch = await provider.fetch([], datetime.now(UTC))
    assert all(e.kind is EventKind.DELISTING and e.asset for e in batch.events)
