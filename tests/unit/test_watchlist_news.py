"""News mentions as a watchlist priority (§7.83, CHANGE.md §4.4)."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from src.analysis.screener import ScreenedSymbol, ScreenMetrics, prioritize_mentioned
from src.core.config import AgentConfig, ContextSettings, MacroCalendarSettings, NewsFeedSpec
from src.core.context import NewsMentionCounter
from src.core.models import NewsItem
from src.core.storage import Storage
from src.core.watchlist import WatchlistManager
from src.data.context import build_context_providers
from src.data.context.news import RssNewsProvider
from tests.unit.test_watchlist import NOW, FakeProvider, _config


def screened(symbol: str, rank: int) -> ScreenedSymbol:
    metrics = ScreenMetrics(momentum=0.1, daily_volatility=0.02, volume_spike=1.0)
    return ScreenedSymbol(symbol=symbol, quote_volume_24h=1e6, metrics=metrics, rank=rank)


def test_prioritize_mentioned_is_stable_and_only_reorders() -> None:
    ranked = [
        screened("A/EUR", 1),
        screened("B/EUR", 2),
        screened("C/EUR", 3),
        screened("D/EUR", 4),
    ]
    out = prioritize_mentioned(ranked, {"C/EUR": 5, "D/EUR": 2, "B/EUR": 1}, min_mentions=2)
    assert [c.symbol for c in out] == ["C/EUR", "D/EUR", "A/EUR", "B/EUR"]
    assert [c.news_mentions for c in out] == [5, 2, 0, 1]
    assert [c.rank for c in out] == [3, 4, 1, 2]  # screener rank kept for audit


@pytest.fixture()
async def storage(tmp_path):
    store = Storage(str(tmp_path / "w.db"), agent="crypto")
    await store.initialize()
    yield store
    await store.close()


def news(title: str, hours: float = 1.0, symbols: list[str] | None = None) -> NewsItem:
    return NewsItem(
        source="cd",
        url=f"https://n/{abs(hash((title, hours)))}",
        title=title,
        published_at=NOW - timedelta(hours=hours),
        symbols=symbols or [],
    )


class TestMentionCounter:
    async def test_counts_over_all_stored_items(self, storage: Storage) -> None:
        await storage.store_news_items(
            [
                news("XLM rallies"),
                news("Stellar partners with a bank"),
                news("XLM and ETH lead"),
                news("Old XLM story", hours=100),
                news("BTC update", symbols=["BTC/EUR"]),
            ]
        )
        counter = NewsMentionCounter(storage, {"XLM/EUR": ["stellar"]}, lookback_hours=48)
        counts = await counter(["XLM/EUR", "ETH/EUR", "SOL/EUR"], NOW)
        assert counts == {"XLM/EUR": 3, "ETH/EUR": 1, "SOL/EUR": 0}


class TestManagerPriority:
    def provider(self) -> FakeProvider:
        # Momentum rank without news: ETH > XLM.
        return FakeProvider(
            volumes={"ETH/EUR": 8e6, "XLM/EUR": 7e6},
            drifts={"ETH/EUR": 0.05, "XLM/EUR": 0.03},
        )

    def manager(self, storage: Storage, counter) -> WatchlistManager:
        return WatchlistManager(
            provider=self.provider(),
            storage=storage,
            config=_config(max_dynamic_symbols=1, news_mentions={"enabled": True}),
            component="crypto",
            quote_currency="EUR",
            mention_counter=counter,
        )

    async def test_mentioned_candidate_takes_the_slot(self, storage: Storage) -> None:
        async def counter(symbols, now):
            return {"XLM/EUR": 3}

        result = await self.manager(storage, counter).refresh(["BTC/EUR"], now=NOW)
        assert result.added == ["XLM/EUR"]
        row = (await storage.get_active_watchlist(now=NOW))[0]
        assert json.loads(row.meta_json)["news_mentions"] == 3

    async def test_counter_failure_keeps_screener_order(self, storage: Storage) -> None:
        async def broken(symbols, now):
            raise RuntimeError("db locked")

        result = await self.manager(storage, broken).refresh(["BTC/EUR"], now=NOW)
        assert result.added == ["ETH/EUR"]

    async def test_without_counter_nothing_changes(self, storage: Storage) -> None:
        result = await self.manager(storage, None).refresh(["BTC/EUR"], now=NOW)
        assert result.added == ["ETH/EUR"]


class TestIngestAndConfig:
    async def test_provider_keeps_unmatched_items_when_asked(self) -> None:
        rss = b"""<rss><channel><item><title>Stellar news</title><link>https://n/1</link>
        <pubDate>Sat, 26 Sep 2026 11:00:00 GMT</pubDate></item></channel></rss>"""

        def make(keep: bool) -> RssNewsProvider:
            return RssNewsProvider(
                httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda r: httpx.Response(200, content=rss))
                ),
                [NewsFeedSpec(name="cd", url="https://feed")],
                {},
                max_items_per_feed=10,
                max_item_chars=100,
                max_age_hours=48,
                max_feed_bytes=10_000,
                keep_unmatched=keep,
            )

        assert (await make(False).fetch(["BTC/EUR"], NOW)).news == []
        kept = (await make(True).fetch(["BTC/EUR"], NOW)).news
        assert [(n.title, n.symbols) for n in kept] == [("Stellar news", [])]

    async def test_builder_passes_the_flag(self) -> None:
        ctx = ContextSettings(
            enabled=True, news={"enabled": True, "feeds": [{"name": "a", "url": "https://a"}]}
        )
        providers, client = build_context_providers(
            ctx, MacroCalendarSettings(feed_url=""), keep_unmatched_news=True
        )
        assert providers[-1]._keep_unmatched is True
        await client.aclose()

    def test_mentions_need_context_news(self) -> None:
        base = {"enabled": True, "pairs": ["BTC/EUR"], "quote_currency": "EUR"}
        watchlist = {"enabled": True, "news_mentions": {"enabled": True}}
        with pytest.raises(ValueError, match="news_mentions needs"):
            AgentConfig(**base, watchlist=watchlist)
        AgentConfig(
            **base,
            watchlist=watchlist,
            context={
                "enabled": True,
                "news": {"enabled": True, "feeds": [{"name": "a", "url": "https://a"}]},
            },
        )

    def test_mention_settings_validated(self) -> None:
        with pytest.raises(ValueError):
            _config(news_mentions={"min_mentions": 0})
        with pytest.raises(ValueError):
            _config(news_mentions={"lookback_hours": 0})


_YAML = """
llm: {{endpoint: "http://localhost:1234/v1/chat/completions", model: m}}
crypto_agent:
  enabled: true
  interval_minutes: 5
  pairs: ["BTC/EUR"]
  quote_currency: EUR
  watchlist: {{enabled: true, news_mentions: {{enabled: {mentions}}}}}
  context:
    enabled: true
    news: {{enabled: true, feeds: [{{name: cd, url: "https://feed.test/rss"}}]}}
macro_calendar: {{feed_url: ""}}
stocks_agent: {{enabled: false, interval_minutes: 60, symbols: ["AAPL"]}}
risk: {{max_position_pct: 0.1}}
storage: {{data_dir: "{db.parent}"}}
monitoring: {{log_level: INFO}}
"""


@pytest.mark.parametrize("mentions", [True, False])
async def test_runner_wires_counter_and_unmatched_ingest(tmp_path, mentions: bool) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.core.config import Settings
    from src.core.runner import run_agent

    config = tmp_path / "settings.yaml"
    config.write_text(_YAML.format(mentions=str(mentions).lower(), db=tmp_path / "r.db"))
    settings = Settings(str(config))
    provider = MagicMock()
    provider.close = AsyncMock()
    executor = MagicMock()
    executor.close = AsyncMock()
    executor.get_positions = AsyncMock(return_value=[])
    manager_cls = MagicMock()
    manager_cls.return_value.refresh = AsyncMock(side_effect=RuntimeError("skip"))
    agent = MagicMock()
    agent.symbols = ["BTC/EUR"]
    agent.run_cycle = AsyncMock(return_value=[])
    agent.shutdown = AsyncMock()
    with (
        patch("src.core.runner.DecisionPipeline"),
        patch("src.core.runner.rehydrate_from_storage", new=AsyncMock()),
        patch("src.core.runner.prune_storage", new=AsyncMock()),
        patch("src.core.runner.WatchlistManager", manager_cls),
        patch("src.data.context.news.get_bytes", new=AsyncMock(side_effect=RuntimeError("off"))),
        patch("src.data.context.build_context_providers", wraps=build_context_providers) as build,
    ):
        await run_agent(
            settings,
            component="crypto",
            agent_enabled=True,
            interval_minutes=5,
            decision_history_limit=10,
            job_id="crypto_cycle",
            build_components=lambda: (provider, executor),
            build_agent=lambda *a: agent,
            run_once=True,
        )
    counter = manager_cls.call_args.kwargs["mention_counter"]
    assert isinstance(counter, NewsMentionCounter) is mentions
    assert build.call_args.kwargs["keep_unmatched_news"] is mentions
