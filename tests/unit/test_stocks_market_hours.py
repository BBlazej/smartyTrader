"""Unit tests for the stocks agent's market-hours guard (pure functions)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.agents.stocks_agent as stocks_agent_module
from src.agents.stocks_agent import (
    StocksAgent,
    is_market_open,
    market_closed_reason,
    parse_holidays,
    parse_market_hours,
)


class TestParseMarketHours:
    def test_parses_range(self) -> None:
        start, end = parse_market_hours("09:00-16:30")
        assert start == time(9, 0)
        assert end == time(16, 30)

    def test_bare_time_is_noop_window(self) -> None:
        start, end = parse_market_hours("24h")
        assert start == time.min
        assert end == time.max

    def test_whitespace_is_stripped(self) -> None:
        start, end = parse_market_hours(" 09:00 - 16:30 ")
        assert start == time(9, 0)
        assert end == time(16, 30)


class TestIsMarketOpen:
    def _at(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 1, 15, hour, minute, tzinfo=UTC)

    def test_within_window(self) -> None:
        assert is_market_open(self._at(10, 30), "09:00-16:30") is True

    def test_at_open_boundary(self) -> None:
        assert is_market_open(self._at(9, 0), "09:00-16:30") is True

    def test_at_close_boundary(self) -> None:
        assert is_market_open(self._at(16, 30), "09:00-16:30") is True

    def test_before_window(self) -> None:
        assert is_market_open(self._at(8, 59), "09:00-16:30") is False

    def test_after_window(self) -> None:
        assert is_market_open(self._at(16, 31), "09:00-16:30") is False

    def test_noop_window_always_open(self) -> None:
        assert is_market_open(self._at(23, 0), "24h") is True
        assert is_market_open(self._at(0, 0), "24h") is True

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        naive = datetime(2026, 1, 15, 12, 0)  # noqa: DTZ001 — deliberately naive
        assert is_market_open(naive, "09:00-16:30") is True

    def test_offset_timezone_is_normalized_to_wall_clock(self) -> None:
        # 14:00 in +02:00 == 12:00 UTC; the local wall clock (14:00) is inside 09:00–16:30.
        plus_two = timezone(timedelta(hours=2))
        local_afternoon = datetime(2026, 1, 15, 14, 0, tzinfo=plus_two)
        assert is_market_open(local_afternoon, "09:00-16:30") is True


class TestWeekendGuard:
    """§7.10: time-of-day alone let a Saturday 10:00 tick run on Friday's stale candles."""

    def test_saturday_inside_window_is_closed(self) -> None:
        saturday = datetime(2026, 1, 17, 10, 30, tzinfo=UTC)  # a Saturday
        assert is_market_open(saturday, "09:00-16:30") is False
        assert market_closed_reason(saturday, "09:00-16:30") == "weekend"

    def test_sunday_inside_window_is_closed(self) -> None:
        sunday = datetime(2026, 1, 18, 12, 0, tzinfo=UTC)  # a Sunday
        assert is_market_open(sunday, "09:00-16:30") is False

    def test_friday_is_unaffected(self) -> None:
        friday = datetime(2026, 1, 16, 10, 30, tzinfo=UTC)  # a Friday
        assert is_market_open(friday, "09:00-16:30") is True
        assert market_closed_reason(friday, "09:00-16:30") is None

    def test_noop_window_stays_open_on_weekend(self) -> None:
        # A bare spec disables the guard entirely — weekends included.
        saturday = datetime(2026, 1, 17, 10, 30, tzinfo=UTC)
        assert is_market_open(saturday, "24h") is True


class TestOvernightWindow:
    """§7.10 config trap: an overnight window made ``start <= t <= end`` never true."""

    def test_evening_inside_wrapped_window(self) -> None:
        # Thursday 23:00 is inside a 22:00-08:00 overnight window.
        evening = datetime(2026, 1, 15, 23, 0, tzinfo=UTC)
        assert is_market_open(evening, "22:00-08:00") is True

    def test_early_morning_inside_wrapped_window(self) -> None:
        morning = datetime(2026, 1, 15, 7, 30, tzinfo=UTC)
        assert is_market_open(morning, "22:00-08:00") is True

    def test_daytime_outside_wrapped_window(self) -> None:
        midday = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        assert is_market_open(midday, "22:00-08:00") is False
        assert market_closed_reason(midday, "22:00-08:00") == "outside trading window"


class TestHolidays:
    def test_parse_holidays(self) -> None:
        assert parse_holidays(["2026-12-24", " 2026-01-01 "]) == {
            date(2026, 12, 24),
            date(2026, 1, 1),
        }
        assert parse_holidays(None) == set()
        assert parse_holidays([]) == set()

    def test_malformed_holiday_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="expected YYYY-MM-DD"):
            parse_holidays(["24/12/2026"])

    def test_holiday_date_is_closed(self) -> None:
        # Thursday 2026-12-24, 10:00 — inside the trading window but a configured holiday.
        now = datetime(2026, 12, 24, 10, 0, tzinfo=UTC)
        assert is_market_open(now, "09:00-16:30", {date(2026, 12, 24)}) is False
        assert market_closed_reason(now, "09:00-16:30", {date(2026, 12, 24)}) == "holiday"

    def test_non_holiday_date_is_open(self) -> None:
        now = datetime(2026, 12, 23, 10, 0, tzinfo=UTC)
        assert is_market_open(now, "09:00-16:30", {date(2026, 12, 24)}) is True


class TestAgentCycleSkip:
    """run_cycle must skip (and name the reason) on weekends and holidays."""

    def _make_agent(self, **kwargs) -> StocksAgent:
        pipeline = MagicMock()
        agent = StocksAgent(
            pipeline=pipeline,
            storage=MagicMock(),
            risk_engine=MagicMock(),
            llm_client=MagicMock(),
            symbols=["AAPL"],
            market_hours="09:00-16:30",
            market_timezone="UTC",  # keep local == UTC so fixtures stay simple
            **kwargs,
        )
        return agent

    async def test_weekend_cycle_is_skipped(self) -> None:
        agent = self._make_agent()
        with patch.object(stocks_agent_module, "datetime") as patched:
            patched.now.return_value = datetime(2026, 1, 17, 10, 0, tzinfo=UTC)  # Saturday
            results = await agent.run_cycle()
        assert results == []
        agent._pipeline.run.assert_not_called()

    async def test_holiday_cycle_is_skipped(self) -> None:
        agent = self._make_agent(market_holidays=["2026-12-24"])
        with patch.object(stocks_agent_module, "datetime") as patched:
            patched.now.return_value = datetime(2026, 12, 24, 10, 0, tzinfo=UTC)  # Thursday
            results = await agent.run_cycle()
        assert results == []
        agent._pipeline.run.assert_not_called()

    def test_malformed_holiday_config_raises_at_construction(self) -> None:
        with pytest.raises(ValueError, match="market_holidays"):
            self._make_agent(market_holidays=["nope"])


class TestLocalNowTimezone:
    """The guard must evaluate against the exchange's local zone, not the host's UTC.

    Regression test for the timezone bug: on a UTC host the old code compared the
    raw UTC wall clock to the Warsaw window, running the guard 1–2h off.
    """

    def _make_agent(self) -> StocksAgent:
        return StocksAgent(
            pipeline=MagicMock(),
            storage=MagicMock(),
            risk_engine=MagicMock(),
            llm_client=MagicMock(),
            symbols=["AAPL"],
            market_hours="09:00-16:30",
            market_timezone="Europe/Warsaw",
        )

    def _local_now_at(self, fixed_utc: datetime) -> datetime:
        with patch.object(stocks_agent_module, "datetime") as patched:
            patched.now.return_value = fixed_utc
            return self._make_agent()._local_now()

    def test_utc_host_time_maps_into_warsaw_window(self) -> None:
        # 08:30 UTC in winter is 09:30 in Warsaw (CET, UTC+1) — inside the window.
        # The old code saw the raw UTC wall clock (08:30) and wrongly skipped.
        local_now = self._local_now_at(datetime(2026, 1, 15, 8, 30, tzinfo=UTC))

        assert local_now.time() == time(9, 30)
        assert is_market_open(local_now, "09:00-16:30") is True

    def test_genuinely_closed_time_stays_closed(self) -> None:
        # 22:00 UTC in summer is 00:00 in Warsaw (CEST, UTC+2) — outside the window.
        local_now = self._local_now_at(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))

        assert local_now.time() == time(0, 0)
        assert is_market_open(local_now, "09:00-16:30") is False


class TestLiveMarketHoursOverride:
    """§7.50: the guard reads its window from the *live* settings object each cycle.

    Before §7.50 a ``market_hours`` safe-config override was written into settings and
    never read again — the agent guarded with its construction-time copy forever.
    """

    def _make_agent(self, live_hours: str | None) -> StocksAgent:
        return StocksAgent(
            pipeline=MagicMock(),
            storage=MagicMock(),
            risk_engine=MagicMock(),
            llm_client=MagicMock(),
            symbols=["AAPL"],
            market_hours="09:00-16:30",
            market_timezone="UTC",  # keep local == UTC so fixtures stay simple
            agent_settings=SimpleNamespace(market_hours=live_hours),
        )

    def _skip_at(self, agent: StocksAgent, when: datetime) -> str | None:
        with patch.object(stocks_agent_module, "datetime") as patched:
            patched.now.return_value = when
            return agent._skip_cycle_reason()

    def test_window_follows_the_live_settings_object(self) -> None:
        wed_0700 = datetime(2026, 1, 14, 7, 0, tzinfo=UTC)  # Wednesday morning
        agent = self._make_agent(live_hours="06:00-08:00")
        assert self._skip_at(agent, wed_0700) is None  # live window covers 07:00

        agent._agent_settings.market_hours = "13:00-14:00"  # override changes…
        assert self._skip_at(agent, wed_0700) == "outside trading window"

    def test_cleared_override_falls_back_to_constructor_window(self) -> None:
        agent = self._make_agent(live_hours=None)
        # No live value → the constructor's 09:00-16:30 governs, not silence.
        assert self._skip_at(agent, datetime(2026, 1, 14, 10, 0, tzinfo=UTC)) is None
        assert self._skip_at(agent, datetime(2026, 1, 14, 7, 0, tzinfo=UTC)) == (
            "outside trading window"
        )
