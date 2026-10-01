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
        bound.bind_venue("paper")
        for value in (1000.0, 1040.0):
            await bound.save_portfolio_snapshot(cash=value, positions_json="[]", total_value=value)
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


async def test_overview_chart_is_loadable_and_valid_json(page_env) -> None:
    import json
    import re

    body = (await page_env.get("/?book=paper_crypto")).text
    # The chart library itself must be loaded, not only its stylesheet.
    assert re.search(r'<script src="[^"]*uplot@1\.[^"]*/uPlot\.iife\.min\.js"', body)
    raw = re.search(
        r'<script id="chart-data" type="application/json">(.*?)</script>', body, re.DOTALL
    ).group(1)
    data = json.loads(raw)  # was HTML-escaped (&#34;) → JSON.parse failed in the browser
    assert set(data) >= {"x", "total_value", "cash", "capital"}
    assert data["capital"] == [1000.0, 1000.0]  # the money the account started with
    json_data = (await page_env.get("/api/portfolio.json?book=paper_crypto")).json()
    assert json_data["capital"] == [1000.0, 1000.0]
    # Readable on the dark card: axis text in light ink (uPlot defaults to black), and
    # the two series in distinct validated palette slots (blue / orange).
    assert "INK = '#c3c2b7'" in body and "stroke: INK" in body
    assert "stroke: '#3987e5'" in body and "stroke: '#d95926'" in body
    assert "label: 'Capital in'" in body and "dash: [6, 4]" in body
    # The Total value card shows the overall return on the capital put in.
    assert re.search(
        r'class="pos">\s*\+4\.00% \(\+40\.00 €\)\s*<span class="muted">vs 1,000\.00 in', body
    )
    assert 'label: "EUR"' in body  # the y axis names the book's currency


async def test_log_page_follows_the_newest_lines(page_env, tmp_path) -> None:
    (tmp_path / "agent_paper_crypto.out.log").write_text("line 1\nline 2\n")
    body = (await page_env.get("/logs/paper_crypto")).text
    assert "htmx:afterSettle" in body and "scrollHeight" in body


def test_times_render_in_the_display_zone() -> None:
    # The DB stores naive UTC; shown raw, every time read 2 h behind a CEST clock while
    # the browser-localized chart looked current.
    from zoneinfo import ZoneInfo

    from src.dashboard.app import _short

    # Naive UTC, as SQLite returns it.
    stored = datetime(2026, 10, 1, 13, 7, 18, tzinfo=UTC).replace(tzinfo=None)
    assert _short(stored, ZoneInfo("Europe/Bratislava")) == "2026-10-01 15:07:18"
    assert _short(stored, ZoneInfo("UTC"), "%H:%M") == "13:07"
    assert _short(None, ZoneInfo("UTC")) == "—"


async def test_pages_use_the_configured_zone(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.dashboard.timezone = "Europe/Bratislava"
    storage = Storage(str(tmp_path / "tz.db"))
    await storage.initialize()
    bound = _bound(storage, "crypto")
    try:
        bound.bind_venue("paper")
        await bound.save_portfolio_snapshot(cash=1.0, positions_json="[]", total_value=1.0)
    finally:
        await bound.close()
    client = _client(create_dashboard_app(settings, _books(storage)))
    try:
        body = (await client.get("/?book=paper_crypto")).text
    finally:
        await client.aclose()
        await storage.close()
    assert "times in CEST" in body or "times in CET" in body


def test_unknown_display_zone_is_rejected() -> None:
    from src.core.config import DashboardSettings

    with pytest.raises(ValueError, match="IANA zone"):
        DashboardSettings(timezone="Mars/Olympus")


async def test_llm_status_card(tmp_path) -> None:
    import httpx

    from src.dashboard.llm_status import LLMProbeCache

    settings = _settings(tmp_path)
    storage = Storage(str(tmp_path / "llm.db"))
    await storage.initialize()
    bound = _bound(storage, "crypto")
    try:
        bound.bind_venue("paper")
        await bound.save_llm_decision(
            symbol="BTC/USDT",
            action="hold",
            confidence=0.7,
            reasoning="x",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
            llm_latency_ms=20_000.0,
            llm_prompt_tokens=2_000,
            llm_completion_tokens=800,
        )
    finally:
        await bound.close()
    models = {"data": [{"id": settings.llm.model, "loaded": True, "context_length": 70000}]}
    probe = LLMProbeCache(
        settings.llm, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=models))
    )
    client = _client(create_dashboard_app(settings, _books(storage), llm_probe=probe))
    try:
        page = (await client.get("/?book=paper_crypto")).text
        card = (await client.get("/partials/llm?book=paper_crypto")).text
    finally:
        await client.aclose()
        await storage.close()
    assert 'hx-get="/partials/llm?book=paper_crypto"' in page  # loaded after the page
    assert "online" in card and "70,000" in card
    assert "configured model:\n      loaded" in card or "configured model: loaded" in " ".join(
        card.split()
    )
    assert "40.0 tok/s" in card and "20.0 s" in card
    _assert_no_secrets(card)
