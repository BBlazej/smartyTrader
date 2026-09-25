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

**§7.50 semantics:** every field resolves as *YAML baseline + override*, re-derived on
every apply — removing an override reverts the live object to its YAML value instead of
leaving a stale one pinned forever. The dashboard/API persist only fields that differ
from the YAML (:func:`strip_noop_overrides`), so saving the form never freezes defaults.
``interval_minutes`` applies immediately: the runner re-arms its APScheduler job when an
apply reports the change, and applies stored overrides *before* scheduling at startup.
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

    §7.50: each field resolves as *override if present, else the YAML baseline*
    (``settings.agent_baselines`` / ``risk_baseline`` / ``execution_baseline``), so an
    apply with the override removed (``raw_json`` empty) reverts live objects to YAML.
    """
    overrides = parse_overrides(raw_json) or SafeConfigOverrides()

    changed: list[str] = []
    agent_settings = getattr(settings, f"{agent_key}_agent", None)
    baseline_agent = (getattr(settings, "agent_baselines", None) or {}).get(agent_key)

    def _effective(field: str) -> Any:
        """Override value if present, else the YAML baseline value (§7.50)."""
        value = getattr(overrides, field, None)
        if value is None and baseline_agent is not None:
            value = getattr(baseline_agent, field, None)
        return value

    def _set(obj: Any, attr: str, value: Any) -> None:
        if value is None or obj is None:
            return
        if getattr(obj, attr, _SENTINEL) != value:
            setattr(obj, attr, value)
            changed.append(attr)

    if agent_settings is not None:
        for field in ("interval_minutes", "market_hours", "decision_history_limit"):
            _set(agent_settings, field, _effective(field))
        for field in ("pairs", "symbols"):
            value = _effective(field)
            if value is not None and list(getattr(agent_settings, field, []) or []) != list(value):
                setattr(agent_settings, field, list(value))
                changed.append(field)

    # Tighten-only at apply time too (§7.43): the YAML may have been tightened after
    # the override was stored — a now-looser stored value is skipped, not applied.
    base_risk = risk_baseline(settings)
    loose = loosened_risk_fields(overrides.risk, base_risk)
    for name in RiskOverride.model_fields:
        if name in loose:
            logger.warning("stored risk override would loosen YAML limit; skipped", field=name)
            continue
        value = getattr(overrides.risk, name, None) if overrides.risk is not None else None
        if value is None and base_risk is not None:
            value = getattr(base_risk, name, None)
        _set(settings.risk, name, value)

    if pipeline is not None:
        executor = pipeline.executor
        base_exec = getattr(settings, "execution_baseline", None) or getattr(
            settings, "execution", None
        )
        for attr, field in (("fee_pct", "paper_fee_pct"), ("slippage_pct", "paper_slippage_pct")):
            value = getattr(overrides.execution, field, None) if overrides.execution else None
            if value is None and base_exec is not None:
                value = getattr(base_exec, field, None)
            if value is not None and hasattr(executor, attr) and getattr(executor, attr) != value:
                setattr(executor, attr, value)
                changed.append(attr)
        history_limit = _effective("decision_history_limit")
        if history_limit is not None and pipeline.decision_history_limit != history_limit:
            pipeline.decision_history_limit = history_limit
            changed.append("decision_history_limit")

    # The agent captured its symbol list at construction; follow the *effective* list
    # so removing a pairs/symbols override reverts it to YAML (§7.50).
    if agent is not None and agent_settings is not None:
        field = "pairs" if agent_key == "crypto" else "symbols"
        effective_symbols = list(getattr(agent_settings, field, []) or [])
        if effective_symbols:
            agent.set_symbols(effective_symbols)
    elif agent is not None:
        # Untyped settings source (tests): legacy override-only path.
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


def strip_noop_overrides(
    overrides: SafeConfigOverrides,
    settings: Settings,
    agent_key: str,
) -> SafeConfigOverrides:
    """Drop override fields equal to their YAML baseline before persisting (§7.50).

    The dashboard form displays merged (YAML ∘ override) values, so a naive save would
    re-persist every field as an override — pinning defaults and silently overriding
    later, stricter YAML edits. Persisting only genuine diffs keeps overrides meaning
    what they say: "differs from the config file".
    """
    data = overrides.model_dump()

    baseline_agent = (getattr(settings, "agent_baselines", None) or {}).get(agent_key)
    if baseline_agent is not None:
        for field in (
            "interval_minutes",
            "market_hours",
            "decision_history_limit",
            "pairs",
            "symbols",
        ):
            value = data.get(field)
            base = getattr(baseline_agent, field, _SENTINEL)
            if value is not None and base is not _SENTINEL and _same(value, base):
                data[field] = None

    base_risk = risk_baseline(settings)
    if base_risk is not None and data.get("risk") is not None:
        kept = {
            name: value
            for name, value in data["risk"].items()
            if value is not None and not _same(value, getattr(base_risk, name, _SENTINEL))
        }
        data["risk"] = kept or None

    base_exec = getattr(settings, "execution_baseline", None)
    if base_exec is not None and data.get("execution") is not None:
        kept = {
            name: value
            for name, value in data["execution"].items()
            if value is not None and not _same(value, getattr(base_exec, name, _SENTINEL))
        }
        data["execution"] = kept or None

    return SafeConfigOverrides(**data)


def _same(value: Any, base: Any) -> bool:
    """Equality that treats lists order-sensitively but type-normalised."""
    if base is _SENTINEL:
        return False
    if isinstance(value, list) or isinstance(base, list):
        return list(value) == list(base)
    return value == base


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
