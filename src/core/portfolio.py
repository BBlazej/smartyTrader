"""The agent's book as the executor reports it — one reader for every caller (§7.79).

Cash + positions alone dip whenever a venue BUY has taken cash the ledger has not
booked yet (a resting limit locks it; a fill whose status poll timed out has spent
it). The executor's optional ``pending_buy_value()`` hook reports that committed
cash; it lands in :attr:`PortfolioState.pending_value` — part of equity, never of
spendable cash.
"""

from __future__ import annotations

from typing import Any

import structlog

from .models import PortfolioState

logger = structlog.get_logger()


def pending_buy_value(executor: Any) -> float:
    """Cash committed to the executor's unbooked BUY orders; 0 without the hook.

    Fail-soft: a broken hook (or a mock) counts as nothing rather than stopping
    the cycle — the dip it would hide is the pre-§7.79 behaviour.
    """
    hook = getattr(executor, "pending_buy_value", None)
    if not callable(hook):
        return 0.0
    try:
        value = hook()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending_buy_value failed; counting 0", error=str(exc))
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(float(value), 0.0)


async def read_portfolio(executor: Any) -> PortfolioState:
    """``PortfolioState`` from the executor: positions, spendable cash, pending BUYs."""
    positions = await executor.get_positions()
    cash = await executor.get_cash()
    return PortfolioState(cash=cash, positions=positions, pending_value=pending_buy_value(executor))
