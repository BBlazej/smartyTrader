"""Agent-side control API (§7.15 P2) — small FastAPI surface, loopback by default.

Served *in-process* next to the agent loop (started by ``core/runner.py`` when
``control_api.enabled``). All control actions go through the DB (the single source
of truth, §7.15 decision #7): endpoints write the ``agent_control`` row and the
agent carries the intent out on its next cycle — so the API is safe to call at any
moment and survives agent restarts.

Safety invariants:

* **No credentials, ever.** Responses are assembled field-by-field from safe
  models; LLM endpoint/model, storage paths and ``.env`` secrets are structurally
  absent. ``PUT /api/config`` accepts only :class:`SafeConfigOverrides` — any
  unknown key (including every credential-shaped one) is rejected wholesale.
* No manual order placement, no live risk override beyond the whitelist, no kill.
* **Browser-safe (§7.43).** ``Host`` must be allowlisted (DNS rebinding) and writes
  with a foreign ``Origin``/``Referer`` are rejected (CSRF from any open web page);
  risk overrides may only tighten the YAML limits.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException

from .config import Settings
from .control_config import (
    agent_config_view,
    parse_overrides,
    risk_baseline,
    safe_config_view,
    strip_noop_overrides,
    validate_overrides_payload,
)
from .storage import Storage
from .web_security import allowed_hosts, install_request_guards

logger = structlog.get_logger()

GetPositions = Callable[[], Awaitable[list[Any]]] | Callable[[], list[Any]]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _row_dict(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in fields:
        value = getattr(row, field, None)
        out[field] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


def create_control_app(
    storage: Storage,
    agent_name: str,
    settings: Settings,
    get_positions: GetPositions | None = None,
) -> FastAPI:
    """Build the control API app for one agent process.

    ``get_positions`` (optional) wires live open positions into the status view —
    pass the executor's ``get_positions``. Without it the status still serves the
    control row + persisted portfolio/decisions.
    """
    app = FastAPI(title=f"trading-agent control ({agent_name})", docs_url=None, redoc_url=None)
    control_cfg = getattr(settings, "control_api", None)
    install_request_guards(
        app,
        allowed_hosts(
            getattr(control_cfg, "host", None), getattr(control_cfg, "allowed_hosts", None)
        ),
    )

    def _guard(agent: str) -> None:
        if agent != agent_name:
            raise HTTPException(
                status_code=404,
                detail=f"this process controls '{agent_name}' only",
            )

    async def _status(agent: str) -> dict[str, Any]:
        control = await storage.get_agent_control(agent)
        positions: list[dict[str, Any]] = []
        if get_positions is not None:
            try:
                raw = await _maybe_await(get_positions())
                positions = [
                    p.model_dump(mode="json") if hasattr(p, "model_dump") else dict(p)
                    for p in (raw or [])
                ]
            except Exception as exc:  # noqa: BLE001 - status must degrade, not fail
                logger.warning("control API position read failed", agent=agent, error=str(exc))
        # Explicit agent scope (§7.39) — correct even if handed an unbound Storage.
        portfolio = await storage.get_latest_portfolio_snapshot(agent=agent)
        decisions = await storage.get_recent_decisions(limit=5, agent=agent)
        return {
            "agent": agent,
            "state": getattr(control, "state", "running"),
            "close_all_requested": bool(getattr(control, "close_all_requested", False)),
            "last_cycle_at": (
                control.last_cycle_at.isoformat()
                if control is not None and control.last_cycle_at is not None
                else None
            ),
            "last_error": getattr(control, "last_error", None),
            "has_config_overrides": bool(
                getattr(control, "config_override_json", None) if control is not None else None
            ),
            "positions": positions,
            "portfolio": _row_dict(
                portfolio, ("cash", "total_value", "unrealized_pnl", "timestamp")
            )
            if portfolio is not None
            else None,
            "recent_decisions": [
                _row_dict(
                    d,
                    (
                        "id",
                        "timestamp",
                        "symbol",
                        "action",
                        "confidence",
                        "risk_verdict",
                        "realized_pnl",
                        "is_fallback",
                    ),
                )
                for d in decisions
            ],
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:  # pragma: no cover - trivial
        return {"ok": True, "agent": agent_name}

    @app.get("/api/agents")
    async def agents() -> list[dict[str, Any]]:
        return [await _status(agent_name)]

    @app.get("/api/agents/{agent}")
    async def agent_status(agent: str) -> dict[str, Any]:
        _guard(agent)
        return await _status(agent)

    @app.get("/api/agents/{agent}/decisions")
    async def decisions(agent: str, limit: int = 50) -> list[dict[str, Any]]:
        _guard(agent)
        limit = max(1, min(limit, 500))
        # Audit view: includes LLM-fallback rows (flagged), unlike prompt context.
        rows = await storage.get_recent_decisions(limit=limit, include_fallback=True, agent=agent)
        return [
            _row_dict(
                row,
                (
                    "id",
                    "timestamp",
                    "symbol",
                    "action",
                    "confidence",
                    "reasoning",
                    "stop_loss",
                    "take_profit",
                    "risk_verdict",
                    "risk_reason",
                    "realized_pnl",
                    "is_fallback",
                ),
            )
            for row in rows
        ]

    @app.get("/api/agents/{agent}/portfolio")
    async def portfolio(agent: str, limit: int = 100) -> dict[str, Any]:
        _guard(agent)
        latest = await storage.get_latest_portfolio_snapshot(agent=agent)
        history = await storage.get_portfolio_history(limit=max(1, min(limit, 1000)), agent=agent)
        return {
            "latest": _row_dict(latest, ("cash", "total_value", "unrealized_pnl", "timestamp"))
            if latest is not None
            else None,
            "history": [
                _row_dict(row, ("cash", "total_value", "unrealized_pnl", "timestamp"))
                for row in history
            ],
        }

    @app.post("/api/agents/{agent}/pause")
    async def pause(agent: str) -> dict[str, Any]:
        _guard(agent)
        await storage.set_agent_state(agent, "paused")
        logger.info("control API: agent paused", agent=agent)
        return {"agent": agent, "state": "paused"}

    @app.post("/api/agents/{agent}/resume")
    async def resume(agent: str) -> dict[str, Any]:
        _guard(agent)
        await storage.set_agent_state(agent, "running")
        logger.info("control API: agent resumed", agent=agent)
        return {"agent": agent, "state": "running"}

    @app.post("/api/agents/{agent}/close-all")
    async def close_all(agent: str) -> dict[str, Any]:
        _guard(agent)
        await storage.request_close_all(agent, requested=True)
        logger.info("control API: close-all latched", agent=agent)
        return {"agent": agent, "close_all_requested": True, "applies": "next cycle"}

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        control = await storage.get_agent_control(agent_name)
        try:
            overrides = parse_overrides(
                getattr(control, "config_override_json", None) if control else None
            )
        except Exception:  # noqa: BLE001 - never fail the view on a corrupt override blob
            overrides = None
        agent_settings = getattr(settings, f"{agent_name}_agent", None)
        return {
            "agent": agent_name,
            "config": safe_config_view(settings, overrides),
            "agent_config": agent_config_view(agent_settings, overrides)
            if agent_settings is not None
            else {},
        }

    @app.put("/api/config")
    async def put_config(payload: dict[str, Any]) -> dict[str, Any]:
        # Reject anything outside the safe whitelist wholesale (unknown keys and
        # every credential-shaped key land here — extra="forbid").
        model, error = validate_overrides_payload(payload, baseline=risk_baseline(settings))
        if model is None:
            raise HTTPException(status_code=400, detail=error)
        # §7.50: persist only fields that genuinely differ from YAML — saving a form
        # pre-filled with defaults must not pin them against later YAML edits.
        model = strip_noop_overrides(model, settings, agent_name)
        dumped = model.model_dump(exclude_none=True)
        stored = model.model_dump_json(exclude_none=True)
        await storage.set_config_override(agent_name, stored if dumped else None)
        logger.info("control API: config overrides saved", agent=agent_name, fields=list(dumped))
        return {
            "agent": agent_name,
            "saved": dumped,
            "applies": "next cycle (interval_minutes re-arms the schedule immediately)",
        }

    return app
