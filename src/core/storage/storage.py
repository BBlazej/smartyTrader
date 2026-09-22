"""The :class:`Storage` facade (§7.36).

Composes the lifecycle core (:class:`StorageBase`) with the data-access mixins so
every public method stays on one importable class — callers keep using
``from src.core.storage import Storage``.
"""

from __future__ import annotations

from .control import ControlMixin
from .decisions import DecisionMixin
from .engine import StorageBase
from .orders import OrderMixin
from .pruning import PruneMixin
from .snapshots import MarketSnapshotMixin, PortfolioSnapshotMixin


class Storage(
    MarketSnapshotMixin,
    DecisionMixin,
    OrderMixin,
    PortfolioSnapshotMixin,
    ControlMixin,
    PruneMixin,
    StorageBase,
):
    """Async repository for all trading data."""
