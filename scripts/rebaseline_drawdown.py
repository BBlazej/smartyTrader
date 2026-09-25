"""One-shot drawdown peak re-baseline (§7.53) — operator-only, CLI-only.

The max-drawdown gate tracks a high-water mark = MAX over all persisted portfolio
snapshots, which never prune. After a long life (or §7.41's spurious early trips) that
latch can sit permanently above reality and freeze every entry while the *real* bankroll
is far below it — with no documented exit other than hand-editing SQLite.

This script is that documented exit::

    python -m scripts.rebaseline_drawdown --agent crypto            # dry run (shows plan)
    python -m scripts/rebaseline_drawdown --agent crypto --yes      # apply at latest equity
    python -m scripts/rebaseline_drawdown --agent stocks --value 95000 --yes

The new baseline defaults to the agent's latest portfolio snapshot value (or pass
``--value``). Applying writes an audited ``drawdown_resets`` row (baseline + timestamp,
plus a structlog line); startup seeding then ignores snapshots older than it. Nothing
trades and no old rows are deleted. The dashboard deliberately offers no such button —
loosening a risk guard is never a web-form click (§7.43's lesson).
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from src.core.config import Settings
from src.core.storage import Storage
from src.monitoring import setup_logging

VALID_AGENTS = ("crypto", "stocks")


async def plan_rebaseline(
    storage: Storage, agent: str, value: float | None = None
) -> tuple[float | None, float]:
    """Resolve (old peak seed, proposed new baseline) without writing anything.

    Raises ``SystemExit`` on unusable input (no history and no explicit value,
    or a non-positive baseline) so the CLI fails before touching the DB.
    """
    latest = await storage.get_latest_portfolio_snapshot(agent=agent)
    if value is None:
        if latest is None:
            raise SystemExit(
                f"no portfolio snapshots stored for agent '{agent}' — pass --value explicitly"
            )
        value = float(latest.total_value)
    if value <= 0:
        raise SystemExit("the new baseline must be a positive portfolio value")

    old_seed = await storage.get_effective_peak_equity(agent=agent)
    return old_seed, value


async def run(agent: str, value: float | None, apply: bool, config_path: str | None) -> None:
    settings = Settings(config_path) if config_path else Settings()
    setup_logging(settings.monitoring.log_level)
    log = structlog.get_logger().bind(component="rebaseline_drawdown")

    storage = Storage(settings.storage.database_path)
    await storage.initialize()
    try:
        old_seed, new_baseline = await plan_rebaseline(storage, agent, value)
        latest = await storage.get_latest_portfolio_snapshot(agent=agent)
        log.info(
            "drawdown re-baseline plan",
            agent=agent,
            previous_peak_seed=old_seed,
            new_baseline=new_baseline,
            latest_equity=None if latest is None else float(latest.total_value),
            applied=apply,
        )
        if apply:
            await storage.record_drawdown_reset(baseline_value=new_baseline, agent=agent)
        else:
            log.warning("dry run — re-run with --yes to persist the re-baseline")
    finally:
        await storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-baseline the max-drawdown high-water mark for one agent (§7.53). "
            "Audited, CLI-only — no dashboard equivalent exists."
        )
    )
    parser.add_argument("--agent", required=True, choices=VALID_AGENTS)
    parser.add_argument(
        "--value",
        type=float,
        default=None,
        help="New baseline (default: the agent's latest portfolio snapshot value)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Actually persist it (default is a dry run)"
    )
    parser.add_argument("--config", default=None, help="Path to an alternative settings.yaml")
    args = parser.parse_args()
    asyncio.run(run(args.agent, args.value, args.yes, args.config))


if __name__ == "__main__":
    main()
