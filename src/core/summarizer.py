"""Batch news summarizer → per-symbol context cards (§7.18, CHANGE.md §4.4 / P5).

Runs on its own cadence, off the trade path. For each traded symbol with news in
the look-back window it asks the LLM for one strict :class:`ContextCard`
(:mod:`src.analysis.context_cards` validates it) and stores it with a TTL. A
symbol is skipped while its latest card is still fresh and no newer item arrived,
so an unchanged news flow costs no LLM calls. Everything is fail-soft per symbol:
a bad reply or an LLM outage only means the previous card ages out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from ..analysis.context_cards import (
    SUMMARIZER_SYSTEM_PROMPT,
    build_summarizer_prompt,
    parse_context_card,
)
from .config import SummarizerSettings

logger = structlog.get_logger()


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


class ContextSummarizer:
    """Turns stored news items into validated context cards."""

    def __init__(
        self,
        *,
        storage: Any,
        llm_client: Any,
        settings: SummarizerSettings,
        component: str,
    ) -> None:
        self._storage = storage
        self._llm = llm_client
        self._settings = settings
        self._component = component

    async def close(self) -> None:
        close = getattr(self._llm, "close", None)
        if close is not None:
            await close()

    async def run(self, symbols: list[str], now: datetime | None = None) -> dict[str, str]:
        """One pass; returns ``{symbol: status}`` for the symbols it looked at."""
        moment = now or datetime.now(UTC)
        settings = self._settings
        status: dict[str, str] = {}
        calls = 0
        for symbol in dict.fromkeys(symbols):
            if calls >= settings.max_symbols_per_run:
                status[symbol] = "deferred (per-run cap)"
                continue
            try:
                items = await self._storage.get_news_for_symbol(
                    symbol,
                    since=moment - timedelta(hours=settings.lookback_hours),
                    limit=settings.max_items_per_card,
                )
                if not items:
                    status[symbol] = "no news"
                    continue
                newest = max(item.published_at for item in items)
                latest = await self._storage.get_latest_context_card_row(symbol)
                if (
                    latest is not None
                    and latest.news_through is not None
                    and latest.news_through >= _naive(newest)
                    and latest.expires_at > _naive(moment)
                ):
                    status[symbol] = "up to date"
                    continue
                calls += 1
                allowed = {item.url for item in items}
                card = await self._llm.ask_json(
                    SUMMARIZER_SYSTEM_PROMPT,
                    build_summarizer_prompt(symbol, items, moment),
                    lambda raw, symbol=symbol, allowed=allowed: parse_context_card(
                        raw, symbol, allowed, moment
                    ),
                    purpose="context_card",
                )
                if card is None:
                    status[symbol] = "failed: no valid card"
                    continue
                await self._storage.store_context_card(
                    card,
                    moment + timedelta(hours=settings.card_ttl_hours),
                    model=getattr(getattr(self._llm, "settings", None), "model", None),
                    news_through=newest,
                )
                status[symbol] = f"card stored ({len(card.sources)} sources)"
            except Exception as exc:  # noqa: BLE001 - one symbol never sinks the pass
                status[symbol] = f"failed: {exc}"
                logger.warning(
                    "context card failed", component=self._component, symbol=symbol, error=str(exc)
                )
        logger.info("context cards refreshed", component=self._component, symbols=status)
        return status
