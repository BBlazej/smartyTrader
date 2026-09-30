"""The dashboard's book registry (§7.78).

One SQLite file per agent × trading mode; the dashboard opens every book it finds in
``storage.data_dir`` (:func:`src.core.db_layout.discover`) and routes each page, latch
write and launch to that file's own :class:`~src.core.storage.Storage`, keyed by
``<mode>_<agent>`` (``demo_crypto``).

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

    key: str  # ``<mode>_<agent>``, e.g. ``demo_crypto``
    mode: str  # paper | demo | real
    agent: str
    storage: Storage


def configured_agents(settings: Settings) -> list[str]:
    return list(getattr(settings.dashboard, "agents", ["crypto", "stocks"]))


async def open_books(settings: Settings) -> list[Book]:
    """Open every ``<mode>_<agent>.db`` book in ``storage.data_dir``.

    The caller owns the storages and must ``close()`` each one.
    """
    agents = configured_agents(settings)
    data_dir = Path(settings.storage.data_dir)
    discovered = db_layout.discover(data_dir, agents)
    if not discovered:
        logger.warning("no books to serve yet", data_dir=str(data_dir))
        return []

    books: list[Book] = []
    for mode, agent, path in discovered:
        storage = Storage(str(path))
        await storage.initialize()
        books.append(Book(key=f"{mode}_{agent}", mode=mode, agent=agent, storage=storage))
    logger.info("books opened", books=[b.key for b in books])
    return books


def find_book(books: list[Book], key: str | None) -> Book | None:
    """The book called ``key`` (``?book=`` / path segment); no key → the first book."""
    if key is not None:
        return next((b for b in books if b.key == key), None)
    return books[0] if books else None
