"""``scripts/backtest.py --event-guard`` calendar selection (§7.82)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scripts.backtest import _event_calendar
from src.core.config import ContextSettings, RiskSettings
from src.core.models import EventKind, MarketEvent
from src.core.storage import Storage

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def settings(context_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        crypto_agent=SimpleNamespace(context=ContextSettings(enabled=context_enabled)),
        risk=RiskSettings(),
    )


@pytest.fixture()
async def storage(tmp_db_path: str):
    s = Storage(tmp_db_path, agent="crypto")
    await s.initialize()
    yield s
    await s.close()


async def _seed(storage: Storage) -> None:
    await storage.store_market_events(
        [
            MarketEvent(
                source="config",
                kind=EventKind.MACRO,
                at=NOW - timedelta(days=3),
                title="FOMC",
                currency="USD",
            ),
            MarketEvent(
                source="okx",
                kind=EventKind.DELISTING,
                at=NOW - timedelta(days=60),
                title="delist X",
                asset="X",
            ),
        ]
    )


async def test_off_never_loads(storage: Storage) -> None:
    await _seed(storage)
    assert await _event_calendar(storage, settings(True), "crypto", "off", NOW, NOW) == (None, None)


async def test_auto_needs_context_enabled_and_stored_events(storage: Storage) -> None:
    start, end = NOW - timedelta(days=7), NOW
    assert await _event_calendar(storage, settings(True), "crypto", "auto", start, end) == (
        None,
        None,
    )
    await _seed(storage)
    assert await _event_calendar(storage, settings(False), "crypto", "auto", start, end) == (
        None,
        None,
    )
    events, since = await _event_calendar(storage, settings(True), "crypto", "auto", start, end)
    # The delisting notice from before the window stays in force (90-day lookback).
    assert {e.title for e in events} == {"FOMC", "delist X"}
    assert since is not None


async def test_on_forces_the_guard(storage: Storage) -> None:
    await _seed(storage)
    events, since = await _event_calendar(
        storage, settings(False), "crypto", "on", NOW - timedelta(days=7), NOW
    )
    assert events and since is not None
