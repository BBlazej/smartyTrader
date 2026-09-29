"""Per-exchange trading windows for a mixed US + EU stocks universe (§7.66)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.agents.stocks_agent as stocks_agent_module
from src.agents.stocks_agent import StocksAgent
from src.core.config import AgentConfig, ExchangeWindow

TUESDAY = (2026, 9, 29)


def agent(**settings_overrides) -> StocksAgent:
    live = SimpleNamespace(
        market_hours="09:30-16:00",
        exchanges={
            "XETR": ExchangeWindow(
                name="XETR",
                market_hours="09:00-17:30",
                market_timezone="Europe/Berlin",
                market_holidays=["2026-10-03"],
            )
        },
        symbol_exchanges={"SAP.DE": "XETR"},
    )
    for key, value in settings_overrides.items():
        setattr(live, key, value)
    pipeline = MagicMock()
    pipeline.run = AsyncMock(side_effect=RuntimeError("stop here"))
    return StocksAgent(
        pipeline=pipeline,
        storage=MagicMock(),
        risk_engine=MagicMock(),
        llm_client=MagicMock(),
        symbols=["AAPL", "SAP.DE"],
        market_hours="09:30-16:00",
        market_timezone="America/New_York",
        agent_settings=live,
    )


def at(hour: int, minute: int = 0, day: tuple[int, int, int] = TUESDAY):
    patcher = patch.object(stocks_agent_module, "datetime")
    patched = patcher.start()
    patched.now.return_value = datetime(*day, hour, minute, tzinfo=UTC)
    return patcher


@pytest.mark.parametrize(
    ("utc_hour", "cycle_skip", "aapl", "sap"),
    [
        (9, False, True, False),  # NY 05:00 closed, Frankfurt 11:00 open
        (15, False, False, False),  # both open
        (19, False, False, True),  # NY 15:00 open, Frankfurt 21:00 closed
        (21, True, True, True),  # both closed → whole cycle skipped
    ],
)
def test_windows_per_exchange(utc_hour: int, cycle_skip: bool, aapl: bool, sap: bool) -> None:
    a = agent()
    patcher = at(utc_hour)
    try:
        assert (a._skip_cycle_reason() is not None) is cycle_skip
        assert (a._skip_symbol_reason("AAPL") is not None) is aapl
        assert (a._skip_symbol_reason("SAP.DE") is not None) is sap
    finally:
        patcher.stop()


def test_exchange_holiday_and_reason_names_the_exchange() -> None:
    a = agent()
    patcher = at(10, day=(2026, 10, 3))  # a Saturday anyway, and a German holiday
    try:
        assert a._skip_symbol_reason("SAP.DE") == "XETR: weekend"
    finally:
        patcher.stop()
    patcher = at(19)
    try:
        assert a._skip_symbol_reason("SAP.DE") == "XETR: outside trading window"
    finally:
        patcher.stop()


def test_without_exchanges_behavior_is_unchanged() -> None:
    a = agent(exchanges={}, symbol_exchanges={})
    patcher = at(9)
    try:
        assert a._skip_cycle_reason() == "outside trading window"
        assert a._skip_symbol_reason("SAP.DE") is None
    finally:
        patcher.stop()


async def test_cycle_runs_only_open_symbols() -> None:
    a = agent()
    patcher = at(9)
    try:
        await a.run_cycle()
    finally:
        patcher.stop()
    assert [c.kwargs["symbol"] for c in a._pipeline.run.call_args_list] == ["SAP.DE"]


class TestConfig:
    def test_valid(self) -> None:
        cfg = AgentConfig(
            enabled=True,
            symbols=["AAPL", "SAP.DE"],
            exchanges={"XETR": {"market_hours": "09:00-17:30", "market_timezone": "Europe/Berlin"}},
            symbol_exchanges={"SAP.DE": "XETR"},
        )
        assert cfg.exchanges["XETR"].market_timezone == "Europe/Berlin"

    @pytest.mark.parametrize(
        "body",
        [
            {"market_hours": "9-17", "market_timezone": "Europe/Berlin"},
            {"market_hours": "09:00-17:30", "market_timezone": "Mars/Base"},
            {"market_hours": "09:00-17:30", "market_timezone": "UTC", "market_holidays": ["x"]},
        ],
    )
    def test_bad_window(self, body: dict) -> None:
        with pytest.raises(ValueError):
            AgentConfig(enabled=True, exchanges={"X": body})

    def test_unknown_exchange_mapping(self) -> None:
        with pytest.raises(ValueError, match="not configured"):
            AgentConfig(enabled=True, symbol_exchanges={"SAP.DE": "XETR"})
