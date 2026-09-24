"""Unit tests for the dashboard's pure view-model builders (§7.15 P3).

No HTTP, no DB — these lock down the win-rate math, chart-array shaping and position
parsing so a rendering bug can't slip through silently.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from src.core.models import Position
from src.dashboard.views import (
    agent_status,
    decision_stats,
    parse_positions,
    portfolio_chart,
    tail_lines,
)


def _dt(year: int, month: int, day: int) -> datetime:
    """Naive-UTC timestamp, matching how SQLite stores snapshot times."""
    return datetime(year, month, day, tzinfo=UTC).replace(tzinfo=None)


def _row(**kw) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _naive(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    """Naive-UTC timestamp with time, matching how SQLite stores heartbeats."""
    return datetime(y, mo, d, h, mi, tzinfo=UTC).replace(tzinfo=None)


class TestAgentStatus:
    """Heartbeat-derived liveness: the state latch alone would show dead agents as running."""

    _NOW = _naive(2026, 9, 21, 12)

    def test_fresh_heartbeat_is_running(self) -> None:
        assert (
            agent_status(
                enabled=True,
                state="running",
                last_cycle_at=self._NOW,
                interval_minutes=5,
                now=self._NOW,
            )
            == "running"
        )

    def test_stale_heartbeat_is_offline_despite_running_latch(self) -> None:
        # 2x interval + 5min grace = 15min for a 5-min agent; one hour is clearly stale.
        assert (
            agent_status(
                enabled=True,
                state="running",
                last_cycle_at=_naive(2026, 9, 21, 11),
                interval_minutes=5,
                now=self._NOW,
            )
            == "offline"
        )

    def test_heartbeat_within_grace_still_running(self) -> None:
        assert (
            agent_status(
                enabled=True,
                state="running",
                last_cycle_at=_naive(2026, 9, 21, 11, 50),
                interval_minutes=5,
                now=self._NOW,
            )
            == "running"
        )

    def test_missing_heartbeat_is_offline(self) -> None:
        assert (
            agent_status(enabled=True, state="running", last_cycle_at=None, now=self._NOW)
            == "offline"
        )

    def test_paused_latch_with_fresh_heartbeat(self) -> None:
        assert (
            agent_status(
                enabled=True,
                state="paused",
                last_cycle_at=self._NOW,
                now=self._NOW,
            )
            == "paused"
        )

    def test_staleness_beats_paused_latch(self) -> None:
        # Paused agents keep beating; a stale beat with a paused latch means the
        # process died while paused — it must read offline, not paused.
        assert (
            agent_status(
                enabled=True,
                state="paused",
                last_cycle_at=_naive(2026, 9, 1),
                now=self._NOW,
            )
            == "offline"
        )

    def test_disabled_wins_over_everything(self) -> None:
        assert (
            agent_status(
                enabled=False,
                state="running",
                last_cycle_at=self._NOW,
                now=self._NOW,
            )
            == "disabled"
        )

    def test_wide_interval_allows_longer_gap(self) -> None:
        # 60-min interval → stale after max(120,10)+5 = 125min; a 90-min-old beat is fine.
        assert (
            agent_status(
                enabled=True,
                state="running",
                last_cycle_at=_naive(2026, 9, 21, 10, 30),
                interval_minutes=60,
                now=self._NOW,
            )
            == "running"
        )


class TestTailLines:
    def test_short_content_untouched(self) -> None:
        assert tail_lines("a\nb", max_lines=10) == "a\nb"

    def test_keeps_last_n_lines(self) -> None:
        content = "\n".join(str(i) for i in range(100))
        assert tail_lines(content, max_lines=3).splitlines() == ["97", "98", "99"]


class TestParsePositions:
    def test_parses_positions_json(self) -> None:
        blob = json.dumps(
            [
                {
                    "symbol": "BTC/USDT",
                    "quantity": 0.5,
                    "avg_entry_price": 50_000.0,
                    "current_price": 51_000.0,
                    "stop_loss": 49_000.0,
                    "take_profit": 53_000.0,
                }
            ]
        )
        positions = parse_positions(_row(positions_json=blob))
        assert len(positions) == 1
        p = positions[0]
        assert isinstance(p, Position)
        assert p.symbol == "BTC/USDT"
        assert p.pnl == 500.0

    def test_blank_or_missing_is_empty(self) -> None:
        assert parse_positions(None) == []
        assert parse_positions(_row(positions_json=None)) == []
        assert parse_positions(_row(positions_json="")) == []

    def test_corrupt_json_degrades_to_empty(self) -> None:
        assert parse_positions(_row(positions_json="{not json")) == []
        assert parse_positions(_row(positions_json='{"symbol": "x"}')) == []  # not a list

    def test_bad_entry_is_skipped_others_kept(self) -> None:
        blob = json.dumps(
            [
                {"symbol": "GOOD", "quantity": 1, "avg_entry_price": 2, "current_price": 3},
                {"nonsense": True},
            ]
        )
        positions = parse_positions(_row(positions_json=blob))
        assert [p.symbol for p in positions] == ["GOOD"]


class TestPortfolioChart:
    def test_orders_oldest_first_and_shapes_arrays(self) -> None:
        # get_portfolio_history returns newest-first; chart must reverse.
        history = [
            _row(timestamp=_dt(2026, 1, 2), total_value=101.0, cash=90.0, unrealized_pnl=11.0),
            _row(timestamp=_dt(2026, 1, 1), total_value=100.0, cash=100.0, unrealized_pnl=0.0),
        ]
        chart = portfolio_chart(history)
        assert chart["total_value"] == [100.0, 101.0]  # oldest → newest
        assert chart["timestamp"][0].startswith("2026-01-01")
        assert len(chart["x"]) == 2
        # Naive UTC epoch: Jan 1 2026 00:00 UTC.
        assert chart["x"][0] < chart["x"][1]

    def test_limit_keeps_most_recent(self) -> None:
        # get_portfolio_history is newest-first: Jan5(105) ... Jan1(101).
        history = [
            _row(
                timestamp=_dt(2026, 1, day),
                total_value=float(100 + day),
                cash=0.0,
                unrealized_pnl=0.0,
            )
            for day in (5, 4, 3, 2, 1)
        ]
        chart = portfolio_chart(history, limit=2)
        # Reversed to oldest-first then trimmed to the two most recent → Jan4, Jan5.
        assert chart["total_value"] == [104.0, 105.0]


class TestDecisionStats:
    def test_win_rate_and_counts(self) -> None:
        rows = [
            _row(
                action="buy",
                confidence=0.8,
                risk_verdict="approved",
                realized_pnl=10.0,
                is_fallback=False,
            ),
            _row(
                action="sell",
                confidence=0.6,
                risk_verdict="approved",
                realized_pnl=-5.0,
                is_fallback=False,
            ),
            _row(
                action="hold",
                confidence=0.2,
                risk_verdict="rejected",
                realized_pnl=None,
                is_fallback=False,
            ),
        ]
        s = decision_stats(rows)
        # §7.46: only the entry (BUY) row is a closed trade; the SELL row's PnL is the
        # same round trip seen from the exit and must not be counted twice.
        assert s["closed"] == 1
        assert s["wins"] == 1 and s["losses"] == 0
        assert s["win_rate"] == 1.0
        assert s["realized_total"] == 10.0
        assert s["buys"] == 1 and s["sells"] == 1 and s["holds"] == 1
        assert s["approved"] == 2 and s["rejected"] == 1

    def test_win_rate_none_when_nothing_closed(self) -> None:
        rows = [
            _row(
                action="buy",
                confidence=0.7,
                risk_verdict="approved",
                realized_pnl=None,
                is_fallback=False,
            )
        ]
        assert decision_stats(rows)["win_rate"] is None

    def test_fallback_excluded_from_actionable_stats(self) -> None:
        rows = [
            _row(
                action="hold",
                confidence=0.0,
                risk_verdict="approved",
                realized_pnl=None,
                is_fallback=True,
            ),
            _row(
                action="buy",
                confidence=0.9,
                risk_verdict="approved",
                realized_pnl=None,
                is_fallback=False,
            ),
        ]
        s = decision_stats(rows)
        assert s["total"] == 2 and s["fallback"] == 1
        # avg confidence over buy/sell only (excludes the fallback hold).
        assert s["avg_confidence"] == 0.9

    def test_confidence_buckets_partition_actionable(self) -> None:
        confs = [0.1, 0.3, 0.5, 0.7, 0.9]
        rows = [
            _row(
                action="buy",
                confidence=c,
                risk_verdict="approved",
                realized_pnl=None,
                is_fallback=False,
            )
            for c in confs
        ]
        s = decision_stats(rows)
        assert sum(b["count"] for b in s["confidence_buckets"]) == 5
        assert [b["count"] for b in s["confidence_buckets"]] == [1, 1, 1, 1, 1]

    def test_confidence_one_lands_in_top_bucket(self) -> None:
        rows = [
            _row(
                action="buy",
                confidence=1.0,
                risk_verdict="approved",
                realized_pnl=None,
                is_fallback=False,
            )
        ]
        s = decision_stats(rows)
        assert [b["count"] for b in s["confidence_buckets"]] == [0, 0, 0, 0, 1]
