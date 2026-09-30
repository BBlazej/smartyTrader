"""The two datetime normalizations (``core/timeutil.py``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from src.core.timeutil import to_naive_utc, to_utc

NAIVE = datetime(2026, 9, 30, 12, 0)  # noqa: DTZ001 - the naive form under test
AWARE = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
CEST = datetime(2026, 9, 30, 14, 0, tzinfo=timezone(timedelta(hours=2)))


def test_to_utc() -> None:
    assert to_utc(NAIVE) == AWARE and to_utc(NAIVE).tzinfo is UTC
    assert to_utc(AWARE) == AWARE
    assert to_utc(CEST).tzinfo is UTC and to_utc(CEST) == AWARE
    assert to_utc(None) is None


def test_to_naive_utc() -> None:
    assert to_naive_utc(AWARE) == NAIVE
    assert to_naive_utc(CEST) == NAIVE
    assert to_naive_utc(NAIVE) is NAIVE
