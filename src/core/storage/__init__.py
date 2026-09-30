"""SQLite persistence layer (§7.36: split from the single ``storage.py``).

The package keeps one public facade — :class:`Storage` — composed from focused
sub-modules (models, engine lifecycle, snapshots, decisions, orders, control,
pruning). Import rows and ``Storage`` from here; internals stay package-private.
"""

from __future__ import annotations

from .control import ControlMixin
from .decisions import DecisionMixin
from .engine import DatabaseIdentityError, StorageBase
from .models import (
    AgentControlRow,
    Base,
    LLMDecisionRow,
    MarketSnapshotRow,
    OrderRow,
    PortfolioSnapshotRow,
)
from .orders import OrderMixin
from .pruning import PruneMixin
from .snapshots import MarketSnapshotMixin, PortfolioSnapshotMixin
from .storage import Storage

__all__ = [
    "AgentControlRow",
    "Base",
    "ControlMixin",
    "DatabaseIdentityError",
    "DecisionMixin",
    "LLMDecisionRow",
    "MarketSnapshotMixin",
    "MarketSnapshotRow",
    "OrderMixin",
    "OrderRow",
    "PortfolioSnapshotMixin",
    "PortfolioSnapshotRow",
    "PruneMixin",
    "Storage",
    "StorageBase",
]
