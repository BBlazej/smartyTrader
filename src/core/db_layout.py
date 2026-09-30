"""One SQLite file per agent × trading mode (§7.78).

Both agents and every account used to share one database, kept apart only by
``agent``/``venue`` filters every query had to remember (§7.39, §7.61, §7.76 — one
missed filter latched the demo account on a paper drawdown peak). Now each
``(mode, agent)`` pair has its own file, so foreign rows are *absent*, not filtered:

    <data_dir>/paper_crypto.db   <data_dir>/demo_crypto.db   <data_dir>/real_crypto.db
    <data_dir>/paper_stocks.db   <data_dir>/demo_stocks.db   <data_dir>/real_stocks.db

The mode is **derived from the executor's venue tag, never configured** — a typo can't
point real trading at a paper file — and every file records its ``(agent, mode)``
identity, which :class:`~src.core.storage.Storage` enforces on open. Files appear only
once a mode is used.
"""

from __future__ import annotations

import re
from pathlib import Path

PAPER = "paper"
DEMO = "demo"
REAL = "real"
MODES: tuple[str, ...] = (PAPER, DEMO, REAL)

#: Venue labels that aren't ``<exchange>-sandbox`` / ``<exchange>-live`` shaped.
_DEMO_VENUES = frozenset({"saxo-sim", "xtb-demo"})
_REAL_VENUES = frozenset({"saxo-live", "xtb-real"})

#: Control-API port offset per mode (§7.78): paper and demo of one agent may run
#: side by side, so each needs its own port (``control_api.<agent>_port + offset``).
CONTROL_PORT_OFFSET: dict[str, int] = {PAPER: 0, DEMO: 10, REAL: 20}

_FILE_RE = re.compile(r"^(paper|demo|real)_([a-z][a-z0-9]*)\.db$")


class UnknownVenueError(ValueError):
    """A venue label whose trading mode can't be told — refused, never guessed."""


def venue_mode(venue: str | None) -> str:
    """Trading mode of an executor's venue tag (§7.61 labels).

    ``None`` (an executor declaring no venue) and ``paper`` are paper.
    ``<exchange>-sandbox``, ``saxo-sim`` and
    ``xtb-demo`` are demo; ``<exchange>-live``, ``saxo-live`` and ``xtb-real`` are real.
    Anything else raises :class:`UnknownVenueError`.
    """
    if venue is None or venue == PAPER:
        return PAPER
    label = venue.lower()
    if label.endswith("-sandbox") or label in _DEMO_VENUES:
        return DEMO
    if label.endswith("-live") or label in _REAL_VENUES:
        return REAL
    raise UnknownVenueError(
        f"cannot tell the trading mode of venue {venue!r} — expected 'paper', "
        "'<exchange>-sandbox'/'-live', 'saxo-sim'/'saxo-live' or 'xtb-demo'/'xtb-real'"
    )


def book_key(mode: str, agent: str) -> str:
    """``demo_crypto``-style name of one agent × mode book (file stem, dashboard key)."""
    if mode not in MODES:
        raise ValueError(f"unknown trading mode {mode!r} (expected one of {MODES})")
    return f"{mode}_{agent}"


def parse_book_key(key: str) -> tuple[str, str] | None:
    """``(mode, agent)`` of a book key or ``None`` when it isn't one."""
    match = _FILE_RE.match(f"{key}.db")
    return (match.group(1), match.group(2)) if match else None


def db_path(data_dir: str | Path, mode: str, agent: str) -> Path:
    """The database file of ``agent`` trading in ``mode``."""
    return Path(data_dir) / f"{book_key(mode, agent)}.db"


def lock_path(data_dir: str | Path, mode: str, agent: str) -> Path:
    """The single-instance runner lock (§7.52) of one agent × mode."""
    return Path(data_dir) / f"{book_key(mode, agent)}.runner.lock"


def discover(data_dir: str | Path, agents: list[str] | None = None) -> list[tuple[str, str, Path]]:
    """Existing ``(mode, agent, path)`` books in ``data_dir``, ordered agent then mode."""
    directory = Path(data_dir)
    if not directory.is_dir():
        return []
    found: list[tuple[str, str, Path]] = []
    for path in directory.iterdir():
        match = _FILE_RE.match(path.name)
        if match is None or not path.is_file():
            continue
        mode, agent = match.group(1), match.group(2)
        if agents is None or agent in agents:
            found.append((mode, agent, path))
    return sorted(found, key=lambda b: (b[1], MODES.index(b[0])))
