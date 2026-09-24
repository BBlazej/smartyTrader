"""The dashboard web app (§7.15 P3–P4) — FastAPI + Jinja2/HTMX.

Reads the shared SQLite DB as a reader (WAL mode → concurrent with the agents'
writes) through :class:`~src.core.storage.Storage`, and writes control intent
(pause / resume / close-all) straight into the ``agent_control`` table via the same
repository methods the agent-side control API uses. The running agents re-read that
row every cycle (:meth:`BaseTradingAgent._handle_control`), so a button press here is
honored on their next tick and survives restarts — no live HTTP coupling to the agent
process, and it works whether or not ``control_api.enabled``.

Safety invariants (inherited from §7.15):

* **No credentials, ever.** Every page is assembled from safe views; config edits go
  through :func:`~src.core.control_config.validate_overrides_payload` → the
  :class:`SafeConfigOverrides` whitelist (``extra="forbid"``), so unknown and every
  credential-shaped key are rejected wholesale.
* No manual order placement, no live risk override beyond the whitelist, no kill.
* **Browser-safe (§7.43).** Every request must carry an allowed ``Host`` (DNS
  rebinding), state-changing requests with a foreign ``Origin``/``Referer`` are
  rejected, and every write must present the per-process CSRF token the pages embed
  (HTMX sends it as ``X-CSRF-Token``; the config form as a hidden field). Risk
  overrides may only tighten the YAML limits.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..core.config import Settings
from ..core.control_config import (
    agent_config_view,
    parse_overrides,
    risk_baseline,
    safe_config_view,
    validate_overrides_payload,
)
from ..core.storage import Storage
from ..core.web_security import (
    CSRF_FIELD,
    CSRF_HEADER,
    allowed_hosts,
    csrf_ok,
    install_request_guards,
    new_csrf_token,
)
from .launch import AgentLauncher
from .views import (
    agent_status,
    decision_stats,
    parse_positions,
    portfolio_chart,
    tail_lines,
)

logger = structlog.get_logger()

_TEMPLATE_DIR = __file__.rsplit("/", 1)[0] + "/templates"


# ── Display filters (Jinja) ───────────────────────────────────


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any) -> str:
    """Format a fraction (0..1) as a percentage; ``None`` → an em dash."""
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _rel(dt_value: Any) -> str:
    """Human 'how long ago' from a naive-UTC timestamp; ``None`` → never."""
    if dt_value is None:
        return "never"
    try:
        then = (
            dt_value.replace(tzinfo=UTC) if getattr(dt_value, "tzinfo", None) is None else dt_value
        )
    except (AttributeError, ValueError):
        return str(dt_value)
    delta = datetime.now(UTC) - then
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86_400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86_400}d ago"


def _short(dt_value: Any) -> str:
    """Short wall-clock rendering (``HH:MM:SS``) of a timestamp, or ``—``."""
    if dt_value is None:
        return "—"
    try:
        return dt_value.strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, ValueError):
        return str(dt_value)


# ── Config form → SafeConfigOverrides payload ─────────────────


def _form_to_payload(form: dict[str, str]) -> dict[str, Any]:
    """Convert a flat dotted-name HTML form into the nested overrides payload.

    Every submitted field is passed through (empty values omitted), so an unexpected
    top-level key — including any credential-shaped one injected into the request —
    survives to be rejected wholesale by ``SafeConfigOverrides`` (``extra="forbid"``).
    Numeric fields are handed over as raw strings and coerced by Pydantic, which is
    also what reports a bad number back to the user.
    """
    payload: dict[str, Any] = {}
    for key, raw in form.items():
        value = raw.strip() if isinstance(raw, str) else raw
        if value == "" or value is None:
            continue  # omit → no override for that field (keeps YAML default)
        *parents, leaf = key.split(".")
        node = payload
        for part in parents:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        if key in ("pairs", "symbols"):
            items = [s.strip() for s in value.split(",") if s.strip()]
            if items:
                node[leaf] = items
        else:
            node[leaf] = value
    return payload


def create_dashboard_app(
    storage: Storage,
    settings: Settings,
    launcher: AgentLauncher | None = None,
) -> FastAPI:
    """Build the dashboard app over an initialized ``storage`` + loaded ``settings``.

    ``launcher`` overrides process supervision (§7.24, tests); when omitted and
    ``dashboard.allow_launch`` is true, a default :class:`AgentLauncher` is built with
    its pid/log files next to the SQLite database.
    """
    app = FastAPI(title="trading-agent dashboard", docs_url=None, redoc_url=None)
    # §7.43: Host allowlist (DNS rebinding) + cross-origin write rejection (CSRF).
    install_request_guards(
        app,
        allowed_hosts(
            getattr(settings.dashboard, "host", None),
            getattr(settings.dashboard, "allowed_hosts", None),
        ),
    )
    csrf_token = new_csrf_token()
    app.state.csrf_token = csrf_token
    templates = Jinja2Templates(directory=_TEMPLATE_DIR)
    templates.env.filters["money"] = _money
    templates.env.filters["pct"] = _pct
    templates.env.filters["rel"] = _rel
    templates.env.filters["short"] = _short
    # Starlette's Jinja2Templates ships no `tojson` (that's Flask); register one so the
    # chart payload can be embedded as JSON. Values are safe, non-user data.
    templates.env.filters["tojson"] = lambda v: _json.dumps(v)

    agents: list[str] = list(getattr(settings.dashboard, "agents", ["crypto", "stocks"]))
    refresh_seconds = int(getattr(settings.dashboard, "refresh_seconds", 5))

    # Data dir shared by the launcher and the log viewer: next to the live SQLite DB.
    data_dir = Path(storage.database_path).parent

    # §7.24 opt-in process supervision: absent unless explicitly allowed by config.
    if launcher is None and getattr(settings.dashboard, "allow_launch", False):
        launcher = AgentLauncher(data_dir=data_dir)
    allow_launch = launcher is not None

    def _check_agent(agent: str) -> None:
        if agent not in agents:
            raise HTTPException(status_code=404, detail=f"unknown agent '{agent}'")

    def _require_csrf(request: Request, form_token: str | None = None) -> None:
        """Every dashboard write must present the token its own pages embed (§7.43)."""
        if not csrf_ok(csrf_token, request.headers.get(CSRF_HEADER) or form_token):
            raise HTTPException(status_code=403, detail="missing or invalid CSRF token")

    def _book_agent(agent: str | None) -> str:
        """Agent whose book a page shows (§7.39): explicit ``?agent=``, else the first
        configured one. Books are per agent — never a blend of both agents' snapshots."""
        if agent is None:
            return agents[0]
        _check_agent(agent)
        return agent

    def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
        base = {
            "request": request,
            "agents": agents,
            "refresh_seconds": refresh_seconds,
            "allow_launch": allow_launch,
            "active": "",
            "csrf_token": csrf_token,
        }
        base.update(extra)
        return base

    async def _health_rows() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for agent in agents:
            control = await storage.get_agent_control(agent)
            agent_cfg = getattr(settings, f"{agent}_agent", None)
            enabled = bool(getattr(agent_cfg, "enabled", False))
            state = getattr(control, "state", "running") if control else "running"
            last_cycle_at = getattr(control, "last_cycle_at", None) if control else None
            # §7.51: LLM outages show on the card, not just in the decisions table.
            try:
                recent = await storage.get_recent_decisions(
                    limit=20, include_fallback=True, agent=agent
                )
            except Exception:  # noqa: BLE001 - a health card must never fail the page
                recent = []
            fallbacks = sum(1 for d in recent if getattr(d, "is_fallback", False))
            rows.append(
                {
                    "name": agent,
                    "enabled": enabled,
                    "state": state,
                    # Effective status: the latch is intent only — a dead agent keeps
                    # its last "running" value, so liveness comes from heartbeat age.
                    "status": agent_status(
                        enabled=enabled,
                        state=state,
                        last_cycle_at=last_cycle_at,
                        interval_minutes=int(getattr(agent_cfg, "interval_minutes", 5) or 5),
                    ),
                    "close_all_requested": bool(
                        getattr(control, "close_all_requested", False) if control else False
                    ),
                    "last_cycle_at": getattr(control, "last_cycle_at", None) if control else None,
                    "last_error": getattr(control, "last_error", None) if control else None,
                    "has_overrides": bool(
                        getattr(control, "config_override_json", None) if control else None
                    ),
                    # §7.24: pid when this dashboard launched/adopted the runner process.
                    "managed_pid": launcher.managed_pid(agent) if launcher else None,
                    "has_log": _agent_log_path(agent).exists(),
                    "fallbacks": fallbacks,
                    "recent_decisions": len(recent),
                }
            )
        return rows

    # ── Pages ─────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request, agent: str | None = None) -> HTMLResponse:
        selected = _book_agent(agent)
        latest = await storage.get_latest_portfolio_snapshot(agent=selected)
        history = await storage.get_portfolio_history(limit=200, agent=selected)
        chart = portfolio_chart(history)
        recent = await storage.get_recent_decisions(limit=8, include_fallback=True, agent=selected)
        return templates.TemplateResponse(
            request,
            "overview.html",
            _ctx(
                request,
                active="overview",
                selected_agent=selected,
                latest=latest,
                chart=chart,
                positions=parse_positions(latest),
                recent_decisions=recent,
                health_rows=await _health_rows(),
            ),
        )

    @app.get("/decisions", response_class=HTMLResponse)
    async def decisions(
        request: Request, limit: int = 200, agent: str | None = None
    ) -> HTMLResponse:
        limit = max(1, min(limit, 1000))
        if agent is not None:
            _check_agent(agent)
        # No ?agent= → every agent's decisions (the table shows which agent made each).
        rows = await storage.get_recent_decisions(limit=limit, include_fallback=True, agent=agent)
        return templates.TemplateResponse(
            request,
            "decisions.html",
            _ctx(
                request,
                active="decisions",
                selected_agent=agent,
                allow_all_agents=True,
                decisions=rows,
                stats=decision_stats(rows),
            ),
        )

    @app.get("/positions", response_class=HTMLResponse)
    async def positions_page(request: Request, agent: str | None = None) -> HTMLResponse:
        selected = _book_agent(agent)
        latest = await storage.get_latest_portfolio_snapshot(agent=selected)
        return templates.TemplateResponse(
            request,
            "positions.html",
            _ctx(
                request,
                active="positions",
                selected_agent=selected,
                latest=latest,
                positions=parse_positions(latest),
            ),
        )

    # ── Agent logs (tail of data/agent_<name>.out.log, §7.24 launches) ───

    def _agent_log_path(agent: str) -> Path:
        # Same data dir the launcher writes to: next to the shared SQLite DB.
        return data_dir / f"agent_{agent}.out.log"

    def _read_log_tail(path: Path, max_bytes: int = 64 * 1024) -> tuple[str, bool]:
        """(tail text, byte-truncated?) — never reads more than the last chunk."""
        try:
            size = path.stat().st_size
            with open(path, "rb") as handle:
                if size > max_bytes:
                    handle.seek(size - max_bytes)
                data = handle.read()
        except OSError:
            return "", False
        text = data.decode(errors="replace")
        if size > max_bytes and "\n" in text:  # drop the partial first line
            text = text.split("\n", 1)[1]
        return tail_lines(text), size > max_bytes

    @app.get("/logs/{agent}", response_class=HTMLResponse)
    async def logs_page(request: Request, agent: str) -> HTMLResponse:
        _check_agent(agent)
        path = _agent_log_path(agent)
        exists = path.exists()
        tail, truncated = _read_log_tail(path) if exists else ("", False)
        return templates.TemplateResponse(
            request,
            "logs.html",
            _ctx(
                request,
                active="logs",
                agent=agent,
                log_path=str(path),
                exists=exists,
                tail=tail,
                truncated=truncated,
            ),
        )

    @app.get("/logs/{agent}/partial", response_class=HTMLResponse)
    async def logs_partial(request: Request, agent: str) -> HTMLResponse:
        _check_agent(agent)
        path = _agent_log_path(agent)
        exists = path.exists()
        tail, truncated = _read_log_tail(path) if exists else ("", False)
        return templates.TemplateResponse(
            request,
            "_log_tail.html",
            _ctx(request, agent=agent, exists=exists, tail=tail, truncated=truncated),
        )

    @app.get("/config/{agent}", response_class=HTMLResponse)
    async def config_page(request: Request, agent: str) -> HTMLResponse:
        _check_agent(agent)
        control = await storage.get_agent_control(agent)
        try:
            overrides = parse_overrides(
                getattr(control, "config_override_json", None) if control else None
            )
        except Exception:  # noqa: BLE001 - never fail the form on a corrupt blob
            overrides = None
        agent_settings = getattr(settings, f"{agent}_agent", None)
        return templates.TemplateResponse(
            request,
            "config.html",
            _ctx(
                request,
                active="config",
                agent=agent,
                config=safe_config_view(settings, overrides),
                agent_config=agent_config_view(agent_settings, overrides)
                if agent_settings is not None
                else {},
                error=None,
                saved=request.query_params.get("saved") == "1",
            ),
        )

    @app.post("/config/{agent}", response_class=HTMLResponse)
    async def config_save(request: Request, agent: str) -> HTMLResponse:
        _check_agent(agent)
        # Parse the urlencoded body ourselves (no python-multipart dep): every
        # submitted key is passed through so an injected credential-shaped field
        # survives to be rejected wholesale by the SafeConfigOverrides whitelist.
        raw_body = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw_body, keep_blank_values=True).items()}
        _require_csrf(request, form.pop(CSRF_FIELD, None))
        payload = _form_to_payload(form)
        model, error = validate_overrides_payload(payload, baseline=risk_baseline(settings))
        if model is None:
            # Re-render the form with the attempted values echoed back + the rejection.
            control = await storage.get_agent_control(agent)
            try:
                overrides = parse_overrides(
                    getattr(control, "config_override_json", None) if control else None
                )
            except Exception:  # noqa: BLE001
                overrides = None
            agent_settings = getattr(settings, f"{agent}_agent", None)
            logger.warning("dashboard config rejected", agent=agent, error=error)
            return templates.TemplateResponse(
                request,
                "config.html",
                _ctx(
                    request,
                    active="config",
                    agent=agent,
                    config=safe_config_view(settings, overrides),
                    agent_config=agent_config_view(agent_settings, overrides)
                    if agent_settings is not None
                    else {},
                    error=error,
                    saved=False,
                ),
                status_code=400,
            )
        stored = model.model_dump_json(exclude_none=True)
        await storage.set_config_override(agent, stored if stored != "{}" else None)
        logger.info("dashboard config saved", agent=agent)
        return RedirectResponse(url=f"/config/{agent}?saved=1", status_code=303)

    # ── Control (writes the DB latch; agent acts next cycle) ───

    @app.post("/control/{agent}/{action}", response_class=HTMLResponse)
    async def control(request: Request, agent: str, action: str) -> HTMLResponse:
        _require_csrf(request)
        _check_agent(agent)
        if action == "pause":
            await storage.set_agent_state(agent, "paused")
        elif action == "resume":
            await storage.set_agent_state(agent, "running")
        elif action == "close-all":
            await storage.request_close_all(agent, requested=True)
        else:  # pragma: no cover - unknown verbs never posted by our UI
            raise HTTPException(status_code=404, detail=f"unknown control action '{action}'")
        logger.info("dashboard control action", agent=agent, action=action)
        # Return the refreshed health fragment so HTMX swaps the cards in place.
        return templates.TemplateResponse(
            request, "_health.html", _ctx(request, health_rows=await _health_rows())
        )

    @app.get("/partials/health", response_class=HTMLResponse)
    async def health_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_health.html", _ctx(request, health_rows=await _health_rows())
        )

    # ── Process supervision (§7.24 — opt-in via dashboard.allow_launch) ───

    @app.post("/launch/{agent}/{action}", response_class=HTMLResponse)
    async def launch(request: Request, agent: str, action: str) -> HTMLResponse:
        _require_csrf(request)
        if launcher is None:  # supervision disabled wholesale
            raise HTTPException(status_code=403, detail="process launching is disabled")
        _check_agent(agent)
        if action == "start":
            cfg = getattr(settings, f"{agent}_agent", None)
            if not getattr(cfg, "enabled", False):
                # The runner would exit at its enabled-gate; starting it is pointless.
                raise HTTPException(status_code=409, detail=f"{agent} agent is disabled in config")
            control = await storage.get_agent_control(agent)
            status = agent_status(
                enabled=True,
                state=getattr(control, "state", "running") if control else "running",
                last_cycle_at=getattr(control, "last_cycle_at", None) if control else None,
                interval_minutes=int(getattr(cfg, "interval_minutes", 5) or 5),
            )
            if status == "running":
                # A fresh heartbeat means an agent is already trading (started
                # elsewhere); launching a second one would double-decide.
                raise HTTPException(
                    status_code=409,
                    detail=f"{agent} already running (fresh heartbeat) — not launching a second",
                )
            try:
                await launcher.start(agent)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        elif action == "stop":
            stopped = await launcher.stop(agent)
            if not stopped:
                raise HTTPException(
                    status_code=409,
                    detail=f"{agent} was not launched by this dashboard — not killing foreign processes",
                )
        else:  # pragma: no cover - unknown verbs never posted by our UI
            raise HTTPException(status_code=404, detail=f"unknown launch action '{action}'")
        logger.info("dashboard launch action", agent=agent, action=action)
        return templates.TemplateResponse(
            request, "_health.html", _ctx(request, health_rows=await _health_rows())
        )

    # ── JSON for the uPlot chart (safe fields only) ───────────

    @app.get("/api/portfolio.json")
    async def portfolio_json(limit: int = 200, agent: str | None = None) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        history = await storage.get_portfolio_history(limit=limit, agent=_book_agent(agent))
        return portfolio_chart(history)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:  # pragma: no cover - trivial
        return {"ok": True}

    return app
