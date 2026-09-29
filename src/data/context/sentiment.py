"""Crypto Fear & Greed index (alternative.me — free, no key) (§7.18).

One market-wide number 0–100 per day with a label ("Extreme Fear" … "Extreme
Greed"). Context for the prompt only — no rule gates on it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ...core.models import SentimentReading
from .base import ContextBatch, safe_label

SOURCE = "fear_greed"


class FearGreedProvider:
    name = SOURCE

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self._client = client
        self._url = url

    async def fetch(self, symbols: list[str], now: datetime) -> ContextBatch:
        response = await self._client.get(self._url)
        response.raise_for_status()
        payload = response.json()
        error = (payload.get("metadata") or {}).get("error")
        if error:
            raise ValueError(f"fear & greed feed error: {error}")
        readings: list[SentimentReading] = []
        for entry in payload.get("data") or []:
            try:
                value = float(entry["value"])
                as_of = datetime.fromtimestamp(int(entry["timestamp"]), tz=UTC)
            except (KeyError, TypeError, ValueError):
                continue
            if not 0.0 <= value <= 100.0:
                continue
            label = entry.get("value_classification")
            readings.append(
                SentimentReading(
                    source=SOURCE,
                    value=value,
                    label=safe_label(label, 40) if isinstance(label, str) else None,
                    as_of=as_of,
                )
            )
        if not readings:
            raise ValueError("fear & greed feed returned no usable readings")
        return ContextBatch(source=SOURCE, sentiment=readings)
