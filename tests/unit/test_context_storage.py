"""Market-context storage + config tests (§7.18).

The tables must be idempotent under repeated refreshes, agent-scoped, windowed
syncs must drop events a calendar feed moved or removed, and retention must
expire old rows. Config blocks validate at load time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.config import (
    ContextSettings,
    MacroCalendarSettings,
    NewsSettings,
    RiskSettings,
    StorageSettings,
    SummarizerSettings,
)
from src.core.models import (
    CardEvent,
    ContextCard,
    EventImportance,
    EventKind,
    MarketEvent,
    NewsItem,
    SentimentReading,
)
from src.core.retention import prune_storage
from src.core.storage import Storage

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def macro(title: str, at: datetime, source: str = "config", currency: str = "USD") -> MarketEvent:
    return MarketEvent(source=source, kind=EventKind.MACRO, at=at, title=title, currency=currency)


@pytest.fixture()
async def storage(tmp_db_path: str):
    s = Storage(tmp_db_path, agent="crypto")
    await s.initialize()
    yield s
    await s.close()


class TestMarketEvents:
    async def test_store_is_idempotent(self, storage: Storage) -> None:
        event = macro("CPI y/y", NOW + timedelta(hours=3))
        assert await storage.store_market_events([event, event]) == 1
        assert await storage.store_market_events([event]) == 0
        rows = await storage.get_market_events(NOW, NOW + timedelta(days=1))
        assert [e.title for e in rows] == ["CPI y/y"]
        assert rows[0].at == NOW + timedelta(hours=3)  # aware again on the way out

    async def test_window_and_asset_filters(self, storage: Storage) -> None:
        await storage.store_market_events(
            [
                macro("FOMC", NOW + timedelta(hours=5)),
                macro("Too late", NOW + timedelta(days=5)),
                MarketEvent(
                    source="okx",
                    kind=EventKind.DELISTING,
                    at=NOW - timedelta(days=2),
                    title="OKX to delist DORA",
                    asset="DORA",
                ),
                MarketEvent(
                    source="yfinance",
                    kind=EventKind.EARNINGS,
                    at=NOW + timedelta(hours=10),
                    title="AAPL earnings",
                    asset="AAPL",
                ),
            ]
        )
        window = await storage.get_market_events(
            NOW - timedelta(days=3), NOW + timedelta(days=1), assets=["btc"]
        )
        # Market-wide events always come along; other assets' events never do.
        assert [e.title for e in window] == ["FOMC"]
        delist = await storage.get_market_events(
            NOW - timedelta(days=3),
            NOW + timedelta(days=1),
            assets=["DORA"],
            kinds=[EventKind.DELISTING],
        )
        assert [e.asset for e in delist] == ["DORA"]

    async def test_sync_drops_moved_events_inside_the_window_only(self, storage: Storage) -> None:
        old_history = macro("Last week NFP", NOW - timedelta(days=6), source="forexfactory")
        moved = macro("GDP", NOW + timedelta(hours=2), source="forexfactory")
        other_source = macro("GDP", NOW + timedelta(hours=2), source="config")
        await storage.store_market_events([old_history, moved, other_source])

        rescheduled = macro("GDP", NOW + timedelta(hours=26), source="forexfactory")
        inserted, deleted = await storage.sync_market_events(
            "forexfactory", [rescheduled], start=NOW - timedelta(days=1)
        )
        assert (inserted, deleted) == (1, 1)
        rows = await storage.get_market_events(NOW - timedelta(days=7), NOW + timedelta(days=2))
        titles = sorted((e.source, e.title, e.at) for e in rows)
        assert ("forexfactory", "Last week NFP", NOW - timedelta(days=6)) in titles
        assert ("config", "GDP", NOW + timedelta(hours=2)) in titles
        assert ("forexfactory", "GDP", NOW + timedelta(hours=26)) in titles
        assert ("forexfactory", "GDP", NOW + timedelta(hours=2)) not in titles

    async def test_agent_scoped(self, tmp_db_path: str, storage: Storage) -> None:
        await storage.store_market_events([macro("FOMC", NOW)])
        other = Storage(tmp_db_path, agent="stocks")
        await other.initialize()
        try:
            assert (
                await other.get_market_events(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
                == []
            )
            # The same event is new for the other agent (per-agent rows).
            assert await other.store_market_events([macro("FOMC", NOW)]) == 1
        finally:
            await other.close()


class TestSentimentAndNews:
    async def test_latest_sentiment(self, storage: Storage) -> None:
        older = SentimentReading(
            source="fear_greed", value=40, label="Fear", as_of=NOW - timedelta(days=1)
        )
        newer = SentimentReading(source="fear_greed", value=73, label="Greed", as_of=NOW)
        assert await storage.store_sentiment([older, newer, newer]) == 2
        latest = await storage.get_latest_sentiment("fear_greed")
        assert latest is not None and latest.value == 73 and latest.as_of == NOW
        assert await storage.get_latest_sentiment("other") is None

    async def test_news_dedup_and_exact_symbol_match(self, storage: Storage) -> None:
        item = NewsItem(
            source="coindesk",
            url="https://example.com/a",
            title="Bitcoin rallies",
            text="body",
            published_at=NOW - timedelta(hours=1),
            symbols=["BTC/EUR"],
        )
        wrapped = item.model_copy(
            update={"url": "https://example.com/b", "title": "WBTC news", "symbols": ["WBTC/EUR"]}
        )
        assert await storage.store_news_items([item, wrapped]) == 2
        assert await storage.store_news_items([item]) == 0
        got = await storage.get_news_for_symbol("BTC/EUR", since=NOW - timedelta(days=1))
        assert [n.title for n in got] == ["Bitcoin rallies"]
        assert got[0].text == "body"
        assert await storage.get_news_for_symbol("BTC/EUR", since=NOW) == []


class TestContextCards:
    def card(self, **update) -> ContextCard:
        base = {
            "symbol": "BTC/EUR",
            "as_of": NOW,
            "sentiment": 0.3,
            "catalysts": ["ETF inflows"],
            "event_risk": [CardEvent(type="macro", date="2026-10-01")],
            "sources": ["https://example.com/a"],
            "confidence": 0.6,
        }
        base.update(update)
        return ContextCard(**base)

    async def test_active_card_respects_ttl(self, storage: Storage) -> None:
        await storage.store_context_card(self.card(), NOW + timedelta(hours=12), model="m")
        assert (await storage.get_active_context_card("BTC/EUR", now=NOW)).sentiment == 0.3
        assert (
            await storage.get_active_context_card("BTC/EUR", now=NOW + timedelta(hours=13)) is None
        )
        assert await storage.get_active_context_card("ETH/EUR", now=NOW) is None

    def test_card_is_strict_and_bounded(self) -> None:
        with pytest.raises(ValueError):
            self.card(action="buy")  # unknown field (e.g. an injected trade)
        with pytest.raises(ValueError):
            self.card(catalysts=["x" * 161])
        with pytest.raises(ValueError):
            self.card(catalysts=["a"] * 6)
        with pytest.raises(ValueError):
            self.card(sources=[])
        with pytest.raises(ValueError):
            self.card(sentiment=1.5)
        with pytest.raises(ValueError):
            self.card(event_risk=[{"type": "buy_now", "date": "2026-10-01"}])


class TestRetention:
    async def test_prune_context_expires_old_rows(self, storage: Storage) -> None:
        old = NOW - timedelta(days=40)
        await storage.store_market_events([macro("old", old), macro("new", NOW)])
        await storage.store_sentiment([SentimentReading(source="fear_greed", value=1, as_of=old)])
        await storage.store_news_items(
            [
                NewsItem(
                    source="s", url="https://x/1", title="t", published_at=old, symbols=["BTC/EUR"]
                )
            ]
        )
        counts = await storage.prune_context(30, now=NOW)
        assert counts == {
            "market_events": 1,
            "sentiment_readings": 1,
            "news_items": 1,
            "context_cards": 0,
        }
        assert await storage.prune_context(0) == {}

    async def test_prune_storage_runs_context_pass(self, storage: Storage) -> None:
        await storage.store_market_events([macro("ancient", datetime(2020, 1, 1, tzinfo=UTC))])
        counts = await prune_storage(storage, StorageSettings(context_retention_days=30))
        assert counts["market_events"] == 1

    def test_negative_retention_rejected(self) -> None:
        with pytest.raises(ValueError):
            StorageSettings(context_retention_days=-1)


class TestContextConfig:
    def test_defaults_are_off(self) -> None:
        ctx = ContextSettings()
        assert ctx.enabled is False
        assert not any(
            (
                ctx.sentiment.enabled,
                ctx.macro.enabled,
                ctx.announcements.enabled,
                ctx.earnings.enabled,
                ctx.news.enabled,
                ctx.summarizer.enabled,
            )
        )

    def test_summarizer_needs_news(self) -> None:
        with pytest.raises(ValueError, match="needs context.news"):
            ContextSettings(summarizer={"enabled": True})

    def test_summarizer_llm_overrides_are_whitelisted(self) -> None:
        assert SummarizerSettings(llm={"model": "small"}).llm_overrides == {"model": "small"}
        with pytest.raises(ValueError, match="forbidden"):
            SummarizerSettings(llm={"api_key": "x"})

    def test_news_needs_feeds_and_valid_urls(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            NewsSettings(enabled=True)
        with pytest.raises(ValueError, match="http"):
            NewsSettings(feeds=[{"name": "x", "url": "file:///etc/passwd"}])
        news = NewsSettings(
            enabled=True,
            feeds=[{"name": "cd", "url": "https://example.com/rss"}],
            aliases={"BTC/EUR": ["Bitcoin"]},
        )
        assert news.aliases == {"BTC/EUR": ["bitcoin"]}

    def test_macro_currencies_validated(self) -> None:
        with pytest.raises(ValueError, match="ISO"):
            ContextSettings(macro={"currencies": ["dollar"]})
        with pytest.raises(ValueError):
            ContextSettings(macro={"min_importance": "huge"})

    def test_macro_calendar_events_parse_to_utc(self) -> None:
        cal = MacroCalendarSettings(
            events=[{"at": "2026-10-28T14:00:00-04:00", "title": "FOMC", "currency": "usd"}]
        )
        assert cal.events == [
            {
                "at": datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
                "title": "FOMC",
                "currency": "USD",
                "importance": "high",
            }
        ]

    @pytest.mark.parametrize(
        "event",
        [
            {"at": "2026-10-28T18:00:00", "title": "no zone", "currency": "USD"},
            {"at": "tomorrow", "title": "x", "currency": "USD"},
            {"at": "2026-10-28T18:00:00Z", "title": "", "currency": "USD"},
            {"at": "2026-10-28T18:00:00Z", "title": "x", "currency": "US"},
            {"at": "2026-10-28T18:00:00Z", "title": "x", "currency": "USD", "where": "DC"},
        ],
    )
    def test_bad_macro_events_rejected(self, event: dict) -> None:
        with pytest.raises(ValueError):
            MacroCalendarSettings(events=[event])

    def test_guard_settings_validated(self) -> None:
        assert RiskSettings().event_guard_enabled is True
        with pytest.raises(ValueError):
            RiskSettings(event_blackout_before_minutes=-1)
        with pytest.raises(ValueError):
            RiskSettings(event_guard_min_importance="urgent")

    def test_event_importance_rank(self) -> None:
        assert EventImportance.LOW.rank < EventImportance.MEDIUM.rank < EventImportance.HIGH.rank
