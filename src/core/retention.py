"""Storage retention pruning (§7.12).

Market snapshots accumulate ~100-candle JSON blobs every cycle; without a bound the
DB grows by tens of MB per day and the Week-6 dashboard/backtester assumptions stop
holding. Both runners call :func:`prune_storage` once at startup (so even ``--once``
cron usage stays hygienic) and again on a scheduled interval while running.

Policy lives in :meth:`Storage.prune`; this module only supplies the
config-reading, fail-soft wrapper shared by both runners.
"""

from __future__ import annotations

import structlog

from .config import StorageSettings
from .storage import Storage


async def prune_storage(storage: Storage, settings: StorageSettings) -> dict[str, int]:
    """Run one retention prune pass according to ``storage`` settings.

    Fail-soft: a pruning failure must never take down a trading cycle's host
    process — it logs a warning and returns an empty result instead of raising.
    Returns the per-table deleted-row counts (empty when nothing is enabled).
    """
    log = structlog.get_logger().bind(component="retention")
    if settings.snapshot_retention_days <= 0 and settings.history_retention_days <= 0:
        log.debug("retention pruning disabled (all windows <= 0)")
        return {}
    try:
        counts = await storage.prune(
            snapshot_days=settings.snapshot_retention_days,
            history_days=settings.history_retention_days,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("retention prune failed (continuing)", error=str(exc))
        return {}
    log.info("retention prune complete", **counts)
    return counts
