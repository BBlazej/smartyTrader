"""Retention pruning (storage mixin, §7.36 / §7.12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from .models import LLMDecisionRow, MarketSnapshotRow, OrderRow


class PruneMixin:
    """``prune`` — deletes expired rows per the storage retention policy."""

    async def prune(self, snapshot_days: int, history_days: int = 0) -> dict[str, int]:
        """Delete rows older than the retention windows; returns ``{table: deleted}``.

        - ``market_snapshots`` older than ``snapshot_days`` — the space hogs (~100-candle
          JSON per symbol-cycle). They are re-creatable cache: the backtester pulls
          fresh candles rather than replaying stored snapshots.
        - ``llm_decisions`` + ``orders`` older than ``history_days`` — the trade record
          (audit trail, fine-tuning dataset, and cooldown rehydration walks closed
          decisions), so this window is opt-in: ``0`` keeps everything.
        - ``portfolio_snapshots`` are **never pruned**: the drawdown high-water seed
          reads MAX over their full history, and pruning them would silently weaken
          that guard after a restart. They are also tiny (no candle blobs).

        A window of ``<= 0`` disables deletion for it; both disabled ⇒ no-op.
        Timestamps compare as naive UTC (SQLite has no tz-aware storage).
        """
        counts: dict[str, int] = {}
        if snapshot_days <= 0 and history_days <= 0:
            return counts
        now = datetime.now(UTC).replace(tzinfo=None)
        async with await self._session() as session:
            if snapshot_days > 0:
                cutoff = now - timedelta(days=snapshot_days)
                result = await session.execute(
                    delete(MarketSnapshotRow).where(MarketSnapshotRow.fetched_at < cutoff)
                )
                counts["market_snapshots"] = result.rowcount or 0
            if history_days > 0:
                cutoff = now - timedelta(days=history_days)
                result = await session.execute(
                    delete(LLMDecisionRow).where(LLMDecisionRow.timestamp < cutoff)
                )
                counts["llm_decisions"] = result.rowcount or 0
                result = await session.execute(delete(OrderRow).where(OrderRow.created_at < cutoff))
                counts["orders"] = result.rowcount or 0
            await session.commit()
        return counts
