"""Storage retention pruning (§7.12).

Market snapshots accumulate ~100-candle JSON blobs every cycle; without a bound the
DB grows by tens of MB per day and the Week-6 dashboard/backtester assumptions stop
holding. Both runners call :func:`prune_storage` once at startup (so even ``--once``
cron usage stays hygienic) and again on a scheduled interval while running.

Policy lives in :meth:`Storage.prune`; this module only supplies the
config-reading, fail-soft wrapper shared by both runners.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import structlog

from .config import StorageSettings
from .storage import Storage


async def prune_storage(storage: Storage, settings: StorageSettings) -> dict[str, int]:
    """Run one retention pass: optional DB backup (§7.35), then pruning (§7.12).

    Fail-soft end to end: neither a failed backup nor a failed prune may take
    down the process that scheduled the job; failures log and carry on.
    Returns the per-table deleted-row counts (empty when nothing is enabled).
    """
    log = structlog.get_logger().bind(component="retention")

    # Snapshot BEFORE deleting anything — a bad prune policy stays recoverable.
    await _maybe_backup(storage, settings, log)

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


async def _maybe_backup(storage: Storage, settings: StorageSettings, log) -> str | None:
    """Write one timestamped backup when ``storage.backup_dir`` is configured (§7.35).

    Returns the backup path (``None`` when disabled or failed — never raises).
    With ``backup_keep > 0`` the oldest backups of this database are rotated out;
    rotation only ever deletes files matching this DB's own name prefix inside
    the configured directory.
    """
    backup_dir = getattr(settings, "backup_dir", "") or ""
    if not backup_dir:
        return None
    try:
        db_stem = Path(storage.database_path).stem
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")
        dest = Path(backup_dir) / f"{db_stem}-{stamp}.db"
        await storage.backup(str(dest))
        kept = _rotate_backups(Path(backup_dir), db_stem, int(getattr(settings, "backup_keep", 0)))
        log.info("database backup written", path=str(dest), backups_kept=kept)
        return str(dest)
    except Exception as exc:  # noqa: BLE001 — a failed backup must not block pruning
        log.warning("database backup failed (continuing)", error=str(exc))
        return None


def _rotate_backups(directory: Path, db_stem: str, keep: int) -> int:
    """Delete oldest-timestamped backups beyond ``keep``; returns files remaining.

    Timestamps sort lexicographically (ISO-ish ``%Y%m%dT%H%M%S.%f``), so name
    order is age order. ``keep <= 0`` disables rotation.
    """
    if keep <= 0:
        return len(list(directory.glob(f"{db_stem}-*.db")))
    backups = sorted(directory.glob(f"{db_stem}-*.db"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:  # pragma: no cover — best-effort housekeeping
            pass
    return min(len(backups), keep)
