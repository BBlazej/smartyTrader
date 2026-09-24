"""Whitelisted *safe* config overrides for the control plane (§7.15).

The dashboard / ``PUT /api/config`` may only change what :class:`SafeConfigOverrides`
allows — intervals, pairs/symbols, market hours, decision-history depth, and the
``risk.*`` / paper-``execution.*`` tuning knobs. **Risk overrides may only tighten**
the YAML limits (§7.43) — a web form must never be able to loosen or switch off a
guard — and ``enforce_exit_levels`` is not on this surface at all. Everything else is rejected by
``extra="forbid"``: unknown keys **and every credential-shaped key** fail the same
way, so secrets can neither be read nor written through this surface. ``llm.*`` and
anything from ``.env`` are deliberately absent — never read, written, or returned.

The same model serves both sides:

* validation at write time (control API / dashboard form),
* application to the live objects each cycle (:func:`parse_and_apply`), mutating the
  shared settings/risk/executor objects the running components already reference.

Deliberate exclusions: ``execution.initial_cash`` (re-seeding a bankroll mid-run is
misleading — restart to re-seed) and anything under ``llm``/``storage.database_path``.
``interval_minutes`` lands in settings for the *next* restart; rescheduling the live
APScheduler job is out of scope for v1 (documented).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:  # avoid an import cycle; only needed for type hints
    from ..agents.base_agent import BaseTradingAgent
    from .config import Settings
    from .decision_pipeline import DecisionPipeline

logger = structlog.get_logger()


class _OverrideBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskOverride(_OverrideBase):
    """``risk.*`` — rule thresholds the dashboard may *tighten* (§7.43).

    ``enforce_exit_levels`` was removed from this surface: switching stop-loss /
    take-profit enforcement off is a YAML edit, never a web-form click.
    """

    max_position_pct: float | None = Field(default=None, gt=0, le=1)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=1)
    max_drawdown_pct: float | None = Field(default=None, gt=0, le=1)
    consecutive_losses_cooldown_minutes: int | None = Field(default=None, ge=0)
    max_open_positions: int | None = Field(default=None, ge=1)
    min_confidence: float | None = Field(default=None, ge=0, le=1)


#: Which direction is *stricter* for each risk override (§7.43): an override may move a
#: limit only this way from the YAML value. "le" = at most the YAML value.
_TIGHTER: dict[str, str] = {
    "max_position_pct": "le",
    "daily_loss_limit_pct": "le",
    "max_drawdown_pct": "le",
    "max_open_positions": "le",
    "min_confidence": "ge",
    "consecutive_losses_cooldown_minutes": "ge",
}

#: Keys once accepted but since removed from the surface; dropped (not rejected) when
#: *reading* stored overrides so a legacy row doesn't disable every other override.
_LEGACY_RISK_KEYS: tuple[str, ...] = ("enforce_exit_levels",)


def loosened_risk_fields(risk: RiskOverride | None, baseline: Any) -> dict[str, str]:
    """``{field: message}`` for override fields looser than the YAML ``baseline``."""
    if risk is None or baseline is None:
        return {}
    problems: dict[str, str] = {}
    for name, direction in _TIGHTER.items():
        value = getattr(risk, name, None)
        base = getattr(baseline, name, None)
        if value is None or base is None:
            continue
        if (value > base) if direction == "le" else (value < base):
            bound = "at most" if direction == "le" else "at least"
            problems[name] = (
                f"risk.{name}={value} would loosen the configured limit ({bound} {base})"
            )
    return problems


def risk_baseline(settings: Any) -> Any:
    """The YAML risk limits overrides are measured against (never the live, mutated ones)."""
    return getattr(settings, "risk_baseline", None) or getattr(settings, "risk", None)


class ExecutionOverride(_OverrideBase):
    """Paper-executor cost model only; ``initial_cash`` is deliberately excluded."""

    paper_fee_pct: float | None = Field(default=None, ge=0, lt=1)
    paper_slippage_pct: float | None = Field(default=None, ge=0, lt=1)


class SafeConfigOverrides(_OverrideBase):
    """The complete safe config surface (§7.15 #6). Unknown/credential keys are rejected."""

    interval_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    pairs: list[str] | None = None
    symbols: list[str] | None = None
    market_hours: str | None = None
    decision_history_limit: int | None = Field(default=None, ge=0, le=100)
    risk: RiskOverride | None = None
    execution: ExecutionOverride | None = None


def safe_config_view(settings: Settings, overrides: SafeConfigOverrides | None) -> dict[str, Any]:
    """The safe config as the dashboard sees it: YAML defaults + stored overrides.

    Only whitelisted fields appear — LLM endpoint/model, storage path and every
    credential are structurally impossible in this payload.
    """
    view: dict[str, Any] = {
        "risk": {name: getattr(settings.risk, name) for name in RiskOverride.model_fields},
        "execution": {
            name: getattr(settings.execution, name) for name in ExecutionOverride.model_fields
        },
    }
    if overrides is not None:
        view["overrides"] = overrides.model_dump(exclude_none=True)
    else:
        view["overrides"] = {}
    return view


def agent_config_view(agent_settings: Any, overrides: SafeConfigOverrides | None) -> dict[str, Any]:
    """Per-agent safe fields (interval/pairs/symbols/market hours/history limit)."""
    base = {
        "interval_minutes": agent_settings.interval_minutes,
        "pairs": list(getattr(agent_settings, "pairs", []) or []),
        "symbols": list(getattr(agent_settings, "symbols", []) or []),
        "market_hours": getattr(agent_settings, "market_hours", None),
        "decision_history_limit": agent_settings.decision_history_limit,
    }
    if overrides is not None:
        base.update(overrides.model_dump(exclude_none=True, exclude={"risk", "execution"}))
    return base


def parse_overrides(raw: str | None) -> SafeConfigOverrides | None:
    """Parse stored override JSON; empty → ``None``. Raises :`ValidationError` on bad content.

    Legacy keys removed from the surface (``risk.enforce_exit_levels``, §7.43) are
    dropped with a warning instead of failing the whole row.
    """
    if not raw or not isinstance(raw, str) or not raw.strip():
        return None
    data = json.loads(raw)
    risk = data.get("risk") if isinstance(data, dict) else None
    if isinstance(risk, dict):
        for key in _LEGACY_RISK_KEYS:
            if key in risk:
                risk.pop(key)
                logger.warning("ignoring legacy stored override", key=f"risk.{key}")
    return SafeConfigOverrides.model_validate(data)


def parse_and_apply(
    settings: Settings,
    agent_key: str,
    raw_json: str | None,
    *,
    pipeline: DecisionPipeline | None = None,
    agent: BaseTradingAgent | None = None,
) -> list[str]:
    """Parse override JSON and apply it to the live objects; returns changed field names.

    Mutates the *same* objects the running components already hold references to:
    ``settings.<agent_key>`` (interval/pairs/symbols/market hours/history limit — read
    per cycle), ``settings.risk`` (the :class:`RiskEngine` holds this very object) and,
    when a pipeline is given, its executor's paper fee/slippage attributes. Raises
    :class:`pydantic.ValidationError` when the stored JSON no longer validates — the
    caller (agent) treats it fail-soft and keeps running on the previous config.
    """
    overrides = parse_overrides(raw_json)
    if overrides is None:
        return []

    changed: list[str] = []
    agent_settings = getattr(settings, f"{agent_key}_agent", None)

    def _set(obj: Any, attr: str, value: Any) -> None:
        if value is None or obj is None:
            return
        if getattr(obj, attr, _SENTINEL) != value:
            setattr(obj, attr, value)
            changed.append(attr)

    if agent_settings is not None:
        _set(agent_settings, "interval_minutes", overrides.interval_minutes)
        if overrides.pairs is not None:
            agent_settings.pairs = list(overrides.pairs)
            changed.append("pairs")
        if overrides.symbols is not None:
            agent_settings.symbols = list(overrides.symbols)
            changed.append("symbols")
        _set(agent_settings, "market_hours", overrides.market_hours)
        _set(agent_settings, "decision_history_limit", overrides.decision_history_limit)

    # Tighten-only at apply time too (§7.43): the YAML may have been tightened after
    # the override was stored — a now-looser stored value is skipped, not applied.
    loose = loosened_risk_fields(overrides.risk, getattr(settings, "risk_baseline", None))
    for name in RiskOverride.model_fields:
        if name in loose:
            logger.warning("stored risk override would loosen YAML limit; skipped", field=name)
            continue
        _set(settings.risk, name, getattr(overrides.risk, name, None))

    if pipeline is not None:
        executor = pipeline.executor
        fee = overrides.execution.paper_fee_pct if overrides.execution else None
        slip = overrides.execution.paper_slippage_pct if overrides.execution else None
        for attr, value in (("fee_pct", fee), ("slippage_pct", slip)):
            if value is not None and hasattr(executor, attr) and getattr(executor, attr) != value:
                setattr(executor, attr, value)
                changed.append(attr)
        if (
            overrides.decision_history_limit is not None
            and pipeline.decision_history_limit != overrides.decision_history_limit
        ):
            pipeline.decision_history_limit = overrides.decision_history_limit
            changed.append("decision_history_limit")

    # The agent captured its symbol list at construction; follow the override.
    if agent is not None:
        new_symbols = (
            overrides.symbols
            if overrides.symbols is not None
            else (overrides.pairs if agent_key == "crypto" and overrides.pairs else None)
        )
        if new_symbols is not None:
            agent.set_symbols(list(new_symbols))

    if changed:
        logger.info("config overrides applied", agent=agent_key, changed=changed)
    return changed


def validate_overrides_payload(
    payload: dict[str, Any],
    baseline: Any = None,
) -> tuple[SafeConfigOverrides, None] | tuple[None, str]:
    """Validate a raw dashboard/API body; returns ``(model, None)`` or ``(None, error)``.

    A single, obvious message for the form layer: any unknown key (including every
    credential-shaped one) is rejected wholesale. With ``baseline`` (the YAML risk
    limits — :func:`risk_baseline`), any risk value looser than it is rejected too.
    """
    try:
        model = SafeConfigOverrides.model_validate(payload)
        problems = loosened_risk_fields(model.risk, baseline)
        if problems:
            return None, "risk overrides may only tighten limits: " + "; ".join(problems.values())
        return model, None
    except ValidationError as exc:  # pragma: no cover - message shape is what matters
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())) or "<body>"
        return (
            None,
            f"invalid or non-whitelisted config key(s): {loc} ({first.get('msg', 'rejected')})",
        )


_SENTINEL = object()
