"""Shared test scaffolding: a minimal settings file, a stub agent, a one-cycle runner.

Tests describe only what differs from :data:`BASE_SETTINGS` (``make_settings``'s
``overrides`` are deep-merged), and drive :func:`src.core.runner.run_agent` through
:func:`run_agent_once` instead of copy-pasting the call and its standard patches.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from src.core.config import Settings
from src.core.runner import run_agent

#: The smallest valid settings.yaml: crypto on (paper, EUR), stocks off.
BASE_SETTINGS: dict[str, Any] = {
    "llm": {"endpoint": "http://localhost:1234/v1/chat/completions", "model": "m"},
    "crypto_agent": {
        "enabled": True,
        "interval_minutes": 5,
        "pairs": ["BTC/EUR"],
        "quote_currency": "EUR",
        "decision_history_limit": 10,
    },
    "stocks_agent": {
        "enabled": False,
        "interval_minutes": 60,
        "symbols": ["AAPL"],
        "decision_history_limit": 10,
    },
    "risk": {
        "max_position_pct": 0.1,
        "daily_loss_limit_pct": 0.02,
        "max_drawdown_pct": 0.05,
        "consecutive_losses_cooldown_minutes": 60,
        "max_open_positions": 5,
        "min_confidence": 0.6,
    },
    "macro_calendar": {"feed_url": ""},
    "monitoring": {"log_level": "INFO"},
}


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def make_settings(tmp_path: Path, overrides: dict[str, Any] | None = None) -> Settings:
    """Write ``BASE_SETTINGS`` deep-merged with ``overrides`` and load it.

    ``storage.data_dir`` defaults to ``tmp_path`` so every book lands there.
    """
    data = _merge(BASE_SETTINGS, {"storage": {"data_dir": str(tmp_path)}})
    data = _merge(data, overrides or {})
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump(data, sort_keys=False))
    return Settings(str(config))


class StubAgent:
    """The agent surface ``run_agent`` touches, recording what the runner did to it."""

    def __init__(
        self, symbols: list[str] | None = None, on_cycle: Callable[[StubAgent], None] | None = None
    ) -> None:
        self._symbols: list[str] = list(symbols or ["BTC/EUR"])
        self.symbol_updates: list[list[str]] = []
        self.sleeves: list[Any] | None = None
        self.applier: Callable[[str | None], None] | None = None
        self.cycles = 0
        self._on_cycle = on_cycle

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbol_updates.append(list(symbols))
        self._symbols = list(symbols)

    def set_sleeves(self, runs: list[Any], book: Any) -> None:
        self.sleeves = runs

    def set_control_overrides_applier(self, applier: Callable[[str | None], None]) -> None:
        self.applier = applier

    async def run_cycle(self) -> list[Any]:
        if self._on_cycle is not None:
            self._on_cycle(self)
        self.cycles += 1
        return []

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def stub_components(provider: Any | None = None, executor: Any | None = None) -> tuple[Any, Any]:
    """A closable provider/executor pair; the executor holds no positions by default."""
    if provider is None:
        provider = MagicMock()
        provider.close = AsyncMock()
    if executor is None:
        executor = MagicMock()
        executor.close = AsyncMock()
        executor.get_positions = AsyncMock(return_value=[])
        executor.get_cash = AsyncMock(return_value=10_000.0)
    return provider, executor


@contextmanager
def runner_patches(*, pipeline: bool = True) -> Iterator[MagicMock | None]:
    """The standard ``run_agent`` isolation: no rehydration, no pruning, optionally a
    mocked ``DecisionPipeline`` (yielded, so tests can read its call kwargs)."""
    with ExitStack() as stack:
        stack.enter_context(patch("src.core.runner.rehydrate_from_storage", new=AsyncMock()))
        stack.enter_context(patch("src.core.runner.prune_storage", new=AsyncMock()))
        pipeline_cls = (
            stack.enter_context(patch("src.core.runner.DecisionPipeline")) if pipeline else None
        )
        yield pipeline_cls


async def run_agent_once(
    settings: Settings,
    agent: Any,
    *,
    provider: Any | None = None,
    executor: Any | None = None,
    component: str = "crypto",
    run_once: bool = True,
    **kwargs: Any,
) -> None:
    """``run_agent`` for one cycle (or the scheduled loop with ``run_once=False`` — the
    caller cancels it) with a stub provider/executor pair.

    Patch with :func:`runner_patches` around the call as the test needs.
    """
    provider, executor = stub_components(provider, executor)
    await run_agent(
        settings,
        component=component,
        agent_enabled=True,
        interval_minutes=5,
        decision_history_limit=10,
        job_id=f"{component}_cycle",
        build_components=lambda: (provider, executor),
        build_agent=lambda pipeline, storage, risk_engine, llm_client: agent,
        run_once=run_once,
        **kwargs,
    )
