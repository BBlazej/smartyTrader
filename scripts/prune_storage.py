"""One-shot storage retention pruning (§7.12).

Runs the same policy the agents schedule (``storage.snapshot_retention_days`` /
``storage.history_retention_days`` in ``config/settings.yaml``) without starting
any agent — useful for reclaiming space on an already-bloated database:

    python -m scripts.prune_storage

The runners also prune at startup and on ``storage.prune_interval_minutes``
while scheduled; this script exists for out-of-band maintenance (cron, one-off
cleanup). Nothing trades; only expired rows are deleted.
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from src.core.config import Settings
from src.core.retention import prune_storage
from src.core.storage import Storage
from src.monitoring import setup_logging


async def run(config_path: str | None = None) -> dict[str, int]:
    settings = Settings(config_path) if config_path else Settings()
    setup_logging(settings.monitoring.log_level)
    log = structlog.get_logger().bind(component="prune_storage")

    storage = Storage(settings.storage.database_path)
    await storage.initialize()
    try:
        counts = await prune_storage(storage, settings.storage)
        if not counts:
            log.info("nothing pruned (retention windows disabled?)")
        return counts
    finally:
        await storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prune expired rows from the SQLite database (no agents, no trades)."
    )
    parser.add_argument("--config", default=None, help="Path to an alternative settings.yaml")
    args = parser.parse_args()
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
