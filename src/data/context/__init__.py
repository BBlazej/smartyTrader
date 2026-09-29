"""Market-context providers (§7.18, CHANGE.md §4.4 / P5).

:func:`build_context_providers` turns one agent's ``context:`` block (plus the shared
``macro_calendar:``) into the enabled providers and the one HTTP client they share.
"""

from __future__ import annotations

import httpx

from ...core.config import ContextSettings, MacroCalendarSettings
from .announcements import OkxAnnouncementsProvider
from .base import ContextBatch, ContextProvider
from .calendar import ConfigMacroProvider, ForexFactoryProvider
from .earnings import EarningsProvider
from .news import RssNewsProvider
from .sentiment import FearGreedProvider

__all__ = [
    "ContextBatch",
    "ContextProvider",
    "build_context_providers",
]


def build_context_providers(
    context: ContextSettings,
    macro_calendar: MacroCalendarSettings,
    keep_unmatched_news: bool = False,
) -> tuple[list[ContextProvider], httpx.AsyncClient]:
    """The enabled providers (in refresh order) and their shared HTTP client.

    ``keep_unmatched_news`` stores news naming no traded symbol too (§7.83 watchlist
    mentions). The caller owns the client and must close it. Nothing is fetched here.
    """
    client = httpx.AsyncClient(
        timeout=context.http_timeout_seconds,
        headers={"User-Agent": context.http_user_agent},
        follow_redirects=True,
    )
    providers: list[ContextProvider] = []
    if context.macro.enabled:
        providers.append(
            ConfigMacroProvider(
                macro_calendar.events, context.macro.currencies, context.macro.min_importance
            )
        )
        if macro_calendar.feed_url:
            providers.append(
                ForexFactoryProvider(
                    client,
                    macro_calendar.feed_url,
                    context.macro.currencies,
                    context.macro.min_importance,
                )
            )
    if context.announcements.enabled:
        providers.append(
            OkxAnnouncementsProvider(
                client, context.announcements.base_url, context.announcements.max_age_days
            )
        )
    if context.earnings.enabled:
        providers.append(EarningsProvider(context.earnings.lookahead_days))
    if context.sentiment.enabled:
        providers.append(FearGreedProvider(client, context.sentiment.url))
    if context.news.enabled:
        news = context.news
        providers.append(
            RssNewsProvider(
                client,
                news.feeds,
                news.aliases,
                max_items_per_feed=news.max_items_per_feed,
                max_item_chars=news.max_item_chars,
                max_age_hours=news.max_age_hours,
                max_feed_bytes=news.max_feed_bytes,
                keep_unmatched=keep_unmatched_news,
            )
        )
    return providers, client
