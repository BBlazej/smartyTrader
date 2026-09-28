"""The dashboard's book registry (§7.78).

One SQLite file per agent × trading mode; the dashboard opens every book it finds in
``storage.data_dir`` (:func:`src.core.db_layout.discover`) and routes each page, latch
write and launch to that file's own :class:`~src.core.storage.Storage`. Before a split
has run there is no ``<mode>_<agent>.db`` file — then the legacy shared
``storage.database_path`` opens as one book *per configured agent* (the pre-§7.78 view:
tabs keyed by agent, no mode badge).

Books are opened unbound (readers + latch writers); row filtering stays per-file, and
the ``agent``/``venue`` columns remain a second layer within it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from ..core import db_layout
from ..core.config import Settings
from ..core.storage import Storage

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Book:
    """One opened book: the file of ``(mode, agent)``, plus its dashboard key."""

    key: str  # ``demo_crypto`` in file mode; ``crypto`` in legacy single-file mode
    mode: str | None  # paper|demo|real, or ``None`` for a pre-§7.78 shared file
    agent: str
    storage: Storage


def configured_agents(settings: Settings) -> list[str]:
    return list(getattr(settings.dashboard, "agents", ["crypto", "stocks"]))


async def open_books(settings: Settings) -> list[Book]:
    """Open every book for the dashboard — discovered files, else the legacy file.

    The caller owns the storages and must ``close()`` each distinct one.
    """
    agents = configured_agents(settings)
    data_dir = Path(settings.storage.data_dir)
    discovered = db_layout.discover(data_dir, agents)
    if not discovered:
        legacy = settings.storage.database_path
        if legacy and legacy != ":memory:" and Path(legacy).exists():
            logger.warning(
                "no <mode>_<agent>.db books found — serving the legacy shared file; "
                "run `python -m scripts.split_database` (§7.78)",
                database=legacy,
            )
            storage = Storage(legacy)
            await storage.initialize()
            return [Book(key=a, mode=None, agent=a, storage=storage) for a in agents]
        logger.warning("no books to serve yet", data_dir=str(data_dir))
        return []

    books: list[Book] = []
    for mode, agent, path in discovered:
        storage = Storage(str(path))
        await storage.initialize()
        books.append(Book(key=f"{mode}_{agent}", mode=mode, agent=agent, storage=storage))
    logger.info("books opened", books=[b.key for b in books])
    return books


def find_book(books: list[Book], *, key: str | None, agent: str | None) -> Book | None:
    """Resolve a request's book: explicit ``?book=`` first, then legacy ``?agent=``.

    A legacy ``?agent=crypto`` link also matches a *book key* (``crypto`` in the
    single-file setup). Matching purely on the agent name is only honored when it is
    unambiguous — with both ``paper_crypto`` and ``demo_crypto`` open, ``?agent=crypto``
    resolves to nothing rather than silently picking one file over another.
    """
    if key is not None:
        return next((b for b in books if b.key == key), None)
    if agent is not None:
        by_key = [b for b in books if b.key == agent]
        if by_key:
            return by_key[0]
        by_agent = [b for b in books if b.agent == agent]
        return by_agent[0] if len(by_agent) == 1 else None
    return books[0] if books else None
