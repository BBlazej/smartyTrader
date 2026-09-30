"""The two datetime normalizations the codebase needs, in one place.

SQLite (via SQLAlchemy's DATETIME) stores naive UTC, while models and clocks carry
tz-aware UTC. Every boundary converts with one of these:

* :func:`to_utc` — naive is taken as UTC; the result is aware UTC.
* :func:`to_naive_utc` — aware is converted to UTC; the result is naive (for
  SQLite comparisons and writes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import overload


@overload
def to_utc(value: datetime) -> datetime: ...
@overload
def to_utc(value: None) -> None: ...
def to_utc(value: datetime | None) -> datetime | None:
    """Aware UTC; a naive value is assumed to be UTC already. ``None`` passes through."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_naive_utc(value: datetime) -> datetime:
    """Naive UTC (SQLite's storage form); a naive value is assumed to be UTC already."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value
