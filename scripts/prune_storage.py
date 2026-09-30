"""One-shot storage retention pruning (§7.12) over the per-mode books (§7.78).

Runs the same policy the agents schedule (``storage.snapshot_retention_days`` /
``storage.history_retention_days`` in ``config/settings.yaml``) without starting
any agent — useful for reclaiming space on an already-bloated book::

    python -m scripts.prune_storage                        # every book in storage.data_dir
    python -m scripts.prune_storage --agent crypto --mode demo   # just data/demo_crypto.db

With no ``--agent``/``--mode`` every ``<mode>_<agent>.db`` found in the data dir is
pruned; with them exactly that one book. The **real**-money books never lose their
decision/order history regardless of ``history_retention_days`` (§7.78).

The runners also prune at startup and on ``storage.prune_interval_minutes``
while scheduled; this script exists for out-of-band maintenance (cron, one-off
cleanup). Nothing trades; only expired rows are deleted.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog

from src.core.config import Settings
from src.core.db_layout import MODES, db_path, discover
from src.core.retention import prune_storage
from src.core.storage import Storage
from src.monitoring import setup_logging


async def run(
    config_path: str | None = None,
    agent: str | None = None,
    mode: str | None = None,
) -> dict[str, int]:
    settings = Settings(config_path) if config_path else Settings()
    setup_logging(settings.monitoring.log_level)
    log = structlog.get_logger().bind(component="prune_storage")

    data_dir = settings.storage.data_dir
    if agent or mode:
        if not (agent and mode):
            raise SystemExit("--agent and --mode belong together (one book, §7.78)")
        book = db_path(data_dir, mode, agent)
        books: list[tuple[str, str, Path]] = [(mode, agent, book)]
    else:
        books = discover(data_dir)
        if not books:
            log.warning("no <mode>_<agent>.db books found", data_dir=str(data_dir))
            return {}

    total: dict[str, int] = {}
    for book_mode, book_agent, path in books:
        storage = Storage(str(path))
        await storage.initialize()
        try:
            counts = await prune_storage(storage, settings.storage, mode=book_mode)
        finally:
            await storage.close()
        log.info(
            "book pruned",
            book=f"{book_mode}_{book_agent}",
            path=str(path),
            **{f"deleted_{table}": n for table, n in counts.items()},
        )
        for table, n in counts.items():
            total[table] = total.get(table, 0) + n
    if not total:
        log.info("nothing pruned (retention windows disabled?)")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prune expired rows from the per-mode SQLite books (no agents, no trades)."
    )
    parser.add_argument("--config", default=None, help="Path to an alternative settings.yaml")
    parser.add_argument(
        "--agent",
        choices=["crypto", "stocks"],
        default=None,
        help="Prune only this agent's book (§7.78; needs --mode)",
    )
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default=None,
        help="Trading mode of the book to prune (paper | demo | real)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.config, args.agent, args.mode))


if __name__ == "__main__":
    main()
