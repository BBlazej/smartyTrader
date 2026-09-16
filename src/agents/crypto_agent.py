"""Crypto agent — orchestrates the full decision cycle for configured pairs.

All shared behavior (cycle loop, risk-tracking updates, persistence, alerts) lives in
:class:`BaseTradingAgent` (§7.13); crypto has no market-hours guard, so this subclass
only fixes the component name and the default timeframe.
"""

from __future__ import annotations

from ..core.decision_pipeline import DecisionPipeline
from ..core.llm_client import LLMClient
from ..core.risk_engine import RiskEngine
from ..core.storage import Storage
from ..monitoring.alerts import AlertManager
from .base_agent import BaseTradingAgent


class CryptoAgent(BaseTradingAgent):
    """Runs decision cycles for a set of crypto pairs (24/7 — no market-hours guard)."""

    def __init__(
        self,
        pipeline: DecisionPipeline,
        storage: Storage,
        risk_engine: RiskEngine,
        llm_client: LLMClient,
        pairs: list[str],
        timeframe: str = "1h",
        alerts: AlertManager | None = None,
    ) -> None:
        super().__init__(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=llm_client,
            symbols=pairs,
            timeframe=timeframe,
            component="crypto_agent",
            alerts=alerts,
        )
