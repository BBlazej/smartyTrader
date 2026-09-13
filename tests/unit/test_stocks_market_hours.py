"""Unit tests for the stocks agent's market-hours guard (pure functions)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch

import src.agents.stocks_agent as stocks_agent_module
from src.agents.stocks_agent import StocksAgent, is_market_open, parse_market_hours


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
