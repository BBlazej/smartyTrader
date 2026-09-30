"""Dashboard market-context page (§7.18): read-only view over the context tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.models import (
    CardEvent,
    ContextCard,
    EventKind,
    MarketEvent,
    NewsItem,
    SentimentReading,
)
from src.core.storage import Storage
from src.dashboard import create_dashboard_app
from src.dashboard.app import _rel
from tests.integration.test_dashboard import _assert_no_secrets, _books, _bound, _client, _settings


@pytest.fixture()
async def page_env(tmp_path):
    settings = _settings(tmp_path)
    storage = Storage(str(tmp_path / "dash.db"))
    await storage.initialize()
    now = datetime.now(UTC)
    bound = _bound(storage, "crypto")
    try:
        await bound.store_market_events(
            [
                MarketEvent(
                    source="config",
                    kind=EventKind.MACRO,
                    at=now + timedelta(minutes=30),
                    title="FOMC rate decision",
                    currency="USD",
                ),
                MarketEvent(
                    source="okx",
                    kind=EventKind.DELISTING,
                    at=now - timedelta(days=3),
                    title="OKX to delist DORA spot pairs",
                    asset="DORA",
                    url="https://my.okx.com/x",
                ),
            ]
        )
        await bound.store_sentiment(
            [SentimentReading(source="fear_greed", value=73, label="Greed", as_of=now)]
        )
        await bound.store_news_items(
            [
                NewsItem(
                    source="coindesk",
                    url="https://n/1",
                    title="<script>alert(1)</script> Bitcoin rallies",
                    published_at=now - timedelta(hours=1),
                    symbols=["BTC/EUR"],
                )
            ]
        )
        await bound.store_context_card(
            ContextCard(
                symbol="BTC/EUR",
                as_of=now,
                sentiment=0.3,
                catalysts=["ETF inflows"],
                event_risk=[CardEvent(type="macro", date="2026-10-28")],
                sources=["https://n/1"],
                confidence=0.6,
            ),
            now + timedelta(hours=6),
            model="small",
        )
    finally:
        await bound.close()
    app = create_dashboard_app(settings, _books(storage))
    client = _client(app)
    yield client
    await client.aclose()
    await storage.close()


async def test_context_page_renders_everything(page_env) -> None:
    response = await page_env.get("/context?book=paper_crypto")
    assert response.status_code == 200
    body = response.text
    for fragment in (
        "Market context — paper_crypto",
        "USD FOMC rate decision",
        "entry blackout",  # 30 min before a high-impact event, by the YAML guard window
        "DORA",
        "OKX to delist DORA spot pairs",
        "73",
        "ETF inflows",
        "Bitcoin rallies",
        "context off",  # this test config has no context block
    ):
        assert fragment in body, fragment
    assert "<script>alert(1)</script>" not in body  # autoescaped
    _assert_no_secrets(body)


async def test_context_page_other_book_is_empty(page_env) -> None:
    body = (await page_env.get("/context?book=paper_stocks")).text
    assert "none on record" in body
    assert "FOMC" not in body


async def test_nav_links_context(page_env) -> None:
    assert 'href="/context"' in (await page_env.get("/")).text


def test_rel_renders_future_times() -> None:
    now = datetime.now(UTC)
    assert _rel(now + timedelta(hours=3, minutes=1)) == "in 3h"
    assert _rel(now - timedelta(days=2, minutes=1)) == "2d ago"
    assert _rel(now + timedelta(seconds=5)) == "just now"
