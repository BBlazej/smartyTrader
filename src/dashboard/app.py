"""One web app over every agent × mode book (§7.15 P3–P4, §7.78) — FastAPI + Jinja2/HTMX.

Since §7.78 each ``(mode, agent)`` pair keeps its own SQLite file, and the dashboard
opens one :class:`~src.dashboard.books.Book` per file found in ``storage.data_dir``.
Every page, latch write and launch targets exactly *one* book's
:class:`~src.core.storage.Storage` — foreign modes' rows are absent from that file,
not filtered out of a shared one. Books are keyed ``<mode>_<agent>``
(``/control/demo_crypto/pause``, ``?book=paper_crypto``, …).

Control intent (pause / resume / close-all) is written straight into that book's
``agent_control`` table via the same repository methods the agent-side control API
uses. The running agents re-read that row every cycle
(:meth:`BaseTradingAgent._handle_control`), so a button press here is honored on their
next tick and survives restarts — no live HTTP coupling to the agent process, and it
works whether or not ``control_api.enabled``.

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

from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

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
    strip_noop_overrides,
    validate_overrides_payload,
)
from ..core.models import EventKind, SymbolContext
from ..core.performance import sleeve_performance
from ..core.risk_engine import RiskEngine
from ..core.storage import Storage
from ..core.timeutil import to_utc
from ..core.web_security import (
    CSRF_FIELD,
    CSRF_HEADER,
    allowed_hosts,
    csrf_ok,
    install_request_guards,
    new_csrf_token,
)
from .books import Book, find_book
from .launch import AgentLauncher
from .llm_status import LLMProbeCache, llm_status_view
from .views import (
    agent_status,
    book_currency,
    capital_return,
    decision_stats,
    parse_positions,
    portfolio_chart,
    signed_money,
    sleeve_rows,
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
        then = to_utc(dt_value)
    except (AttributeError, ValueError):
        return str(dt_value)
    delta = datetime.now(UTC) - then
    secs = int(delta.total_seconds())
    if -60 < secs < 0:
        return "just now"
    # Future timestamps (upcoming events, card expiry — §7.18) read "in …".
    fmt = "in {}" if secs < 0 else "{} ago"
    secs = abs(secs)
    if secs < 60:
        return fmt.format(f"{secs}s")
    if secs < 3600:
        return fmt.format(f"{secs // 60}m")
    if secs < 86_400:
        return fmt.format(f"{secs // 3600}h")
    return fmt.format(f"{secs // 86_400}d")


def _short(dt_value: Any, tz: tzinfo | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Wall-clock rendering of a stored timestamp in the display zone, or ``—``.

    The DB stores naive UTC; shown raw, every time read 1–2 h behind a Central
    European clock (and the chart, which the browser localizes). ``tz=None`` → the
    host's local zone.
    """
    if dt_value is None:
        return "—"
    try:
        return to_utc(dt_value).astimezone(tz).strftime(fmt)
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
    settings: Settings,
    books: list[Book],
    launcher: AgentLauncher | None = None,
    llm_probe: LLMProbeCache | None = None,
) -> FastAPI:
    """Build the dashboard app over loaded ``settings`` and one book per SQLite file.

    §7.78 book model: every page, latch write and launch targets one *book*
    (``(mode, agent)``, from :func:`~src.dashboard.books.open_books`). ``launcher``
    overrides process supervision (§7.24, tests); when omitted and
    ``dashboard.allow_launch`` is true, a default :class:`AgentLauncher` is built with
    its pid/log files in ``storage.data_dir`` (keyed per book). ``llm_probe`` replaces
    the LLM server probe behind the overview's LLM status card (tests).
    """
    llm_probe = llm_probe or LLMProbeCache(settings.llm)
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
    templates.env.filters["signed_money"] = signed_money
    templates.env.filters["rel"] = _rel
    display_tz = ZoneInfo(settings.dashboard.timezone) if settings.dashboard.timezone else None
    templates.env.filters["short"] = lambda value, fmt="%Y-%m-%d %H:%M:%S": _short(
        value, display_tz, fmt
    )
    # Shown in the header so a time is never ambiguous ("CEST").
    templates.env.globals["tz_label"] = lambda: datetime.now(display_tz).strftime("%Z")
    # ``tojson`` is Jinja's built-in: HTML-safe JSON (Markup, not re-escaped), so the
    # chart payload embedded in <script type="application/json"> stays valid JSON.

    refresh_seconds = int(getattr(settings.dashboard, "refresh_seconds", 5))

    # Data dir shared by the launcher and the log viewer: where the book files live.
    data_dir = Path(settings.storage.data_dir)

    # §7.24 opt-in process supervision: absent unless explicitly allowed by config.
    if launcher is None and getattr(settings.dashboard, "allow_launch", False):
        launcher = AgentLauncher(data_dir=data_dir)
    allow_launch = launcher is not None

    def _book_views() -> list[dict[str, Any]]:
        return [{"key": b.key, "mode": b.mode, "agent": b.agent} for b in books]

    def _require_book(key: str | None = None) -> Book:
        """Resolve a request's book (``?book=`` or path segment); unknown → 404."""
        book = find_book(books, key)
        if book is None:
            raise HTTPException(status_code=404, detail=f"unknown book '{key}'")
        return book

    def _require_csrf(request: Request, form_token: str | None = None) -> None:
        """Every dashboard write must present the token its own pages embed (§7.43)."""
        if not csrf_ok(csrf_token, request.headers.get(CSRF_HEADER) or form_token):
            raise HTTPException(status_code=403, detail="missing or invalid CSRF token")

    def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
        base = {
            "request": request,
            "books": _book_views(),
            "refresh_seconds": refresh_seconds,
            "allow_launch": allow_launch,
            "active": "",
            "csrf_token": csrf_token,
        }
        base.update(extra)
        return base

    async def _health_rows() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for book in books:
            control = await book.storage.get_agent_control(book.agent)
            agent_cfg = getattr(settings, f"{book.agent}_agent", None)
            enabled = bool(getattr(agent_cfg, "enabled", False))
            state = getattr(control, "state", "running") if control else "running"
            # §7.51: LLM outages show on the card, not just in the decisions table.
            try:
                recent = await book.storage.get_recent_decisions(
                    limit=20, include_fallback=True, agent=book.agent
                )
            except Exception:  # noqa: BLE001 - a health card must never fail the page
                recent = []
            fallbacks = sum(1 for d in recent if getattr(d, "is_fallback", False))
            # §7.69: per-decision LLM latency percentiles (p50/p95) from stored rows.
            try:
                llm_stats = await book.storage.get_llm_latency_stats(limit=100, agent=book.agent)
            except Exception:  # noqa: BLE001 - a health card must never fail the page
                llm_stats = None
            rows.append(
                {
                    # Card name = book key (``demo_crypto``).
                    "name": book.key,
                    "agent": book.agent,
                    "mode": book.mode,
                    "enabled": enabled,
                    "state": state,
                    # Effective status: the latch is intent only — a dead agent keeps
                    # its last "running" value, so liveness comes from heartbeat age.
                    "status": agent_status(
                        enabled=enabled,
                        state=state,
                        last_cycle_at=getattr(control, "last_cycle_at", None) if control else None,
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
                    "managed_pid": launcher.managed_pid(book.key) if launcher else None,
                    "has_log": _agent_log_path(book.key).exists(),
                    "fallbacks": fallbacks,
                    "recent_decisions": len(recent),
                    "llm_stats": llm_stats,
                }
            )
        return rows

    async def _starting_capital(book: Any, latest: Any) -> float | None:
        """Capital the book's current account started with: its oldest snapshot on the
        latest snapshot's venue. The DB records no deposits, so later top-ups are not
        included."""
        if latest is None:
            return None
        first = await book.storage.get_first_portfolio_snapshot(
            agent=book.agent, venue=latest.venue
        )
        return float(first.total_value) if first is not None else None

    # ── Pages ─────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request, book: str | None = None) -> HTMLResponse:
        selected = _require_book(book)
        latest = await selected.storage.get_latest_portfolio_snapshot(agent=selected.agent)
        history = await selected.storage.get_portfolio_history(limit=200, agent=selected.agent)
        capital = await _starting_capital(selected, latest)
        chart = portfolio_chart(history, capital=capital)
        recent = await selected.storage.get_recent_decisions(
            limit=8, include_fallback=True, agent=selected.agent
        )
        return templates.TemplateResponse(
            request,
            "overview.html",
            _ctx(
                request,
                active="overview",
                selected_key=selected.key,
                latest=latest,
                capital=capital,
                capital_return=capital_return(latest.total_value if latest else None, capital),
                capital_gain=(latest.total_value - capital) if latest and capital else None,
                currency=book_currency(settings, selected.agent),
                chart=chart,
                positions=parse_positions(latest),
                recent_decisions=recent,
                health_rows=await _health_rows(),
            ),
        )

    @app.get("/decisions", response_class=HTMLResponse)
    async def decisions(
        request: Request,
        limit: int = 200,
        book: str | None = None,
    ) -> HTMLResponse:
        limit = max(1, min(limit, 1000))
        selected_key: str | None = None
        if book is not None:
            selected = _require_book(book)
            selected_key = selected.key
            rows = await selected.storage.get_recent_decisions(
                limit=limit, include_fallback=True, agent=selected.agent
            )
        else:
            # No selection → every book's decisions (the table shows agent + file).
            # One read per *distinct* storage (tests may share one handle).
            storages: dict[int, Storage] = {}
            for b in books:
                storages.setdefault(id(b.storage), b.storage)
            parts = [
                await s.get_recent_decisions(limit=limit, include_fallback=True)
                for s in storages.values()
            ]
            # Rows are naive-UTC; untimed ones sort last. The sentinel matches them.
            naive_min = datetime.min  # noqa: DTZ901
            rows = sorted(
                (row for part in parts for row in part),
                key=lambda r: (r.timestamp is not None, r.timestamp or naive_min),
                reverse=True,
            )[:limit]
        return templates.TemplateResponse(
            request,
            "decisions.html",
            _ctx(
                request,
                active="decisions",
                selected_key=selected_key,
                allow_all_books=True,
                decisions=rows,
                stats=decision_stats(rows),
            ),
        )

    async def _sleeve_view(
        book: Book, symbols: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Per-sleeve table + ``symbol → sleeve`` (§7.71); empty without sleeves."""
        agent = book.agent
        try:
            snapshots = await book.storage.get_latest_sleeve_snapshots(agent=agent)
            if not snapshots:
                return [], {}
            allocation = await book.storage.get_latest_allocation(agent=agent)
            since = allocation.created_at if allocation is not None else None
            peaks: dict[str, float | None] = {}
            performance: dict[str, Any] = {}
            for snap in snapshots:
                if since is not None:
                    peaks[snap.strategy] = await book.storage.get_effective_sleeve_peak(
                        snap.strategy, since, agent=agent
                    )
                # §7.73: the sleeve's ledger since its allocation.
                performance[snap.strategy] = sleeve_performance(
                    snap.strategy,
                    await book.storage.get_strategy_orders(snap.strategy, since, agent=agent),
                    await book.storage.get_sleeve_equity_series(snap.strategy, since, agent=agent),
                )
            owners = await book.storage.get_position_strategies(symbols, agent=agent)
        except Exception:  # the sleeve table must never fail the page
            logger.warning("sleeve view unavailable", agent=agent, exc_info=True)
            return [], {}
        return sleeve_rows(snapshots, allocation, peaks, performance), owners

    @app.get("/positions", response_class=HTMLResponse)
    async def positions_page(request: Request, book: str | None = None) -> HTMLResponse:
        selected = _require_book(book)
        latest = await selected.storage.get_latest_portfolio_snapshot(agent=selected.agent)
        positions = parse_positions(latest)
        sleeves, owners = await _sleeve_view(selected, [p.symbol for p in positions])
        return templates.TemplateResponse(
            request,
            "positions.html",
            _ctx(
                request,
                active="positions",
                selected_key=selected.key,
                latest=latest,
                positions=positions,
                sleeves=sleeves,
                owners=owners,
            ),
        )

    # ── Market context (§7.18), read-only ───────────────────────────────

    @app.get("/context", response_class=HTMLResponse)
    async def context_page(request: Request, book: str | None = None) -> HTMLResponse:
        selected = _require_book(book)
        store = selected.storage
        now = datetime.now(UTC)
        agent_cfg = getattr(settings, f"{selected.agent}_agent", None)
        context_cfg = getattr(agent_cfg, "context", None)
        view: dict[str, Any] = {
            "enabled": bool(getattr(context_cfg, "enabled", False)),
            "events": [],
            "notices": [],
            "sentiment": None,
            "cards": [],
            "news": [],
            "freshness": {},
            "blackout": None,
            "error": None,
        }
        try:
            view["events"] = await store.get_market_events(
                now - timedelta(days=1),
                now + timedelta(days=7),
                kinds=[EventKind.MACRO, EventKind.EARNINGS],
                agent=selected.agent,
            )
            view["notices"] = await store.get_market_events(
                now - timedelta(days=120),
                now + timedelta(days=1),
                kinds=[EventKind.DELISTING],
                agent=selected.agent,
            )
            sentiment_source = getattr(getattr(context_cfg, "sentiment", None), "source", None)
            if sentiment_source:
                view["sentiment"] = await store.get_latest_sentiment(
                    sentiment_source, agent=selected.agent
                )
            view["cards"] = await store.get_active_context_cards(now, agent=selected.agent)
            view["news"] = await store.get_recent_news(
                now - timedelta(hours=24), limit=30, agent=selected.agent
            )
            view["freshness"] = await store.get_context_freshness(agent=selected.agent)
            # Market-wide blackout right now, by the agent's own guard settings.
            view["blackout"] = RiskEngine(settings.risk).event_blackout_reason(
                SymbolContext(
                    symbol="*",
                    now=now,
                    events=[e for e in view["events"] if e.kind is EventKind.MACRO],
                )
            )
        except Exception:  # the page must render even over a pre-§7.18 file
            logger.warning("context view unavailable", book=selected.key, exc_info=True)
            view["error"] = "market context tables unreadable — see the dashboard log"
        return templates.TemplateResponse(
            request,
            "context.html",
            _ctx(request, active="context", selected_key=selected.key, now=now, **view),
        )

    # ── Agent logs (tail of data/agent_<book>.out.log, §7.24 launches) ───

    def _agent_log_path(key: str) -> Path:
        # Same data dir the launcher writes to, keyed per book (§7.78).
        return data_dir / f"agent_{key}.out.log"

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

    @app.get("/logs/{key}", response_class=HTMLResponse)
    async def logs_page(request: Request, key: str) -> HTMLResponse:
        book = _require_book(key)
        path = _agent_log_path(book.key)
        exists = path.exists()
        tail, truncated = _read_log_tail(path) if exists else ("", False)
        return templates.TemplateResponse(
            request,
            "logs.html",
            _ctx(
                request,
                active="logs",
                agent=book.key,
                log_path=str(path),
                exists=exists,
                tail=tail,
                truncated=truncated,
            ),
        )

    @app.get("/logs/{key}/partial", response_class=HTMLResponse)
    async def logs_partial(request: Request, key: str) -> HTMLResponse:
        book = _require_book(key)
        path = _agent_log_path(book.key)
        exists = path.exists()
        tail, truncated = _read_log_tail(path) if exists else ("", False)
        return templates.TemplateResponse(
            request,
            "_log_tail.html",
            _ctx(request, agent=book.key, exists=exists, tail=tail, truncated=truncated),
        )

    @app.get("/config/{key}", response_class=HTMLResponse)
    async def config_page(request: Request, key: str) -> HTMLResponse:
        book = _require_book(key)
        control = await book.storage.get_agent_control(book.agent)
        try:
            overrides = parse_overrides(
                getattr(control, "config_override_json", None) if control else None
            )
        except Exception:  # noqa: BLE001 - never fail the form on a corrupt blob
            overrides = None
        agent_settings = getattr(settings, f"{book.agent}_agent", None)
        return templates.TemplateResponse(
            request,
            "config.html",
            _ctx(
                request,
                active="config",
                agent=book.key,
                config=safe_config_view(settings, overrides),
                agent_config=agent_config_view(agent_settings, overrides)
                if agent_settings is not None
                else {},
                error=None,
                saved=request.query_params.get("saved") == "1",
            ),
        )

    @app.post("/config/{key}", response_class=HTMLResponse)
    async def config_save(request: Request, key: str) -> HTMLResponse:
        book = _require_book(key)
        # Parse the urlencoded body ourselves (no python-multipart dep): every
        # submitted key is passed through so an injected credential-shaped field
        # survives to be rejected wholesale by the SafeConfigOverrides whitelist.
        raw_body = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw_body, keep_blank_values=True).items()}
        _require_csrf(request, form.pop(CSRF_FIELD, None))
        payload = _form_to_payload(form)
        model, error = validate_overrides_payload(
            payload,
            baseline=risk_baseline(settings),
            quote_currency=getattr(
                getattr(settings, f"{book.agent}_agent", None), "quote_currency", None
            ),
        )
        if model is None:
            # Re-render the form with the attempted values echoed back + the rejection.
            control = await book.storage.get_agent_control(book.agent)
            try:
                overrides = parse_overrides(
                    getattr(control, "config_override_json", None) if control else None
                )
            except Exception:  # noqa: BLE001
                overrides = None
            agent_settings = getattr(settings, f"{book.agent}_agent", None)
            logger.warning("dashboard config rejected", agent=book.key, error=error)
            return templates.TemplateResponse(
                request,
                "config.html",
                _ctx(
                    request,
                    active="config",
                    agent=book.key,
                    config=safe_config_view(settings, overrides),
                    agent_config=agent_config_view(agent_settings, overrides)
                    if agent_settings is not None
                    else {},
                    error=error,
                    saved=False,
                ),
                status_code=400,
            )
        # §7.50: the form shows merged (YAML ∘ override) values, so strip fields that
        # merely echo the YAML baseline — a save then persists only genuine overrides
        # and never pins defaults against later, stricter YAML edits.
        model = strip_noop_overrides(model, settings, book.agent)
        dumped = model.model_dump(exclude_none=True)
        stored = model.model_dump_json(exclude_none=True)
        await book.storage.set_config_override(book.agent, stored if dumped else None)
        logger.info("dashboard config saved", agent=book.key, fields=list(dumped))
        return RedirectResponse(url=f"/config/{book.key}?saved=1", status_code=303)

    # ── Control (writes the book's DB latch; its agent acts next cycle) ───

    @app.post("/control/{key}/{action}", response_class=HTMLResponse)
    async def control(request: Request, key: str, action: str) -> HTMLResponse:
        _require_csrf(request)
        book = _require_book(key)
        if action == "pause":
            await book.storage.set_agent_state(book.agent, "paused")
        elif action == "resume":
            await book.storage.set_agent_state(book.agent, "running")
        elif action == "close-all":
            await book.storage.request_close_all(book.agent, requested=True)
        else:  # pragma: no cover - unknown verbs never posted by our UI
            raise HTTPException(status_code=404, detail=f"unknown control action '{action}'")
        logger.info("dashboard control action", agent=book.key, action=action)
        # Return the refreshed health fragment so HTMX swaps the cards in place.
        return templates.TemplateResponse(
            request, "_health.html", _ctx(request, health_rows=await _health_rows())
        )

    @app.get("/partials/llm", response_class=HTMLResponse)
    async def llm_partial(request: Request, book: str | None = None) -> HTMLResponse:
        """The overview's LLM status card: a live server probe (cached) plus the
        selected book's stored per-decision LLM numbers (§7.69)."""
        selected = _require_book(book)
        try:
            stats = await selected.storage.get_llm_latency_stats(limit=50, agent=selected.agent)
        except Exception:  # noqa: BLE001 - a status card must never fail the page
            stats = None
        view = llm_status_view(await llm_probe.get(), stats, settings.llm.max_tokens)
        return templates.TemplateResponse(
            request, "_llm_status.html", _ctx(request, llm=view, selected_key=selected.key)
        )

    @app.get("/partials/health", response_class=HTMLResponse)
    async def health_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_health.html", _ctx(request, health_rows=await _health_rows())
        )

    # ── Process supervision (§7.24 — opt-in via dashboard.allow_launch) ───

    @app.post("/launch/{key}/{action}", response_class=HTMLResponse)
    async def launch(request: Request, key: str, action: str) -> HTMLResponse:
        _require_csrf(request)
        if launcher is None:  # supervision disabled wholesale
            raise HTTPException(status_code=403, detail="process launching is disabled")
        book = _require_book(key)
        if action == "start":
            cfg = getattr(settings, f"{book.agent}_agent", None)
            if not getattr(cfg, "enabled", False):
                # The runner would exit at its enabled-gate; starting it is pointless.
                raise HTTPException(
                    status_code=409, detail=f"{book.agent} agent is disabled in config"
                )
            control_row = await book.storage.get_agent_control(book.agent)
            status = agent_status(
                enabled=True,
                state=getattr(control_row, "state", "running") if control_row else "running",
                last_cycle_at=getattr(control_row, "last_cycle_at", None) if control_row else None,
                interval_minutes=int(getattr(cfg, "interval_minutes", 5) or 5),
            )
            if status == "running":
                # A fresh heartbeat means an agent is already trading (started
                # elsewhere); launching a second one would double-decide.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{book.key} already running (fresh heartbeat) — not launching a second"
                    ),
                )
            try:
                await launcher.start(book.key)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        elif action == "stop":
            stopped = await launcher.stop(book.key)
            if not stopped:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{book.key} was not launched by this dashboard — "
                        "not killing foreign processes"
                    ),
                )
        else:  # pragma: no cover - unknown verbs never posted by our UI
            raise HTTPException(status_code=404, detail=f"unknown launch action '{action}'")
        logger.info("dashboard launch action", agent=book.key, action=action)
        return templates.TemplateResponse(
            request, "_health.html", _ctx(request, health_rows=await _health_rows())
        )

    # ── JSON for the uPlot chart (safe fields only) ───────────

    @app.get("/api/portfolio.json")
    async def portfolio_json(limit: int = 200, book: str | None = None) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        selected = _require_book(book)
        history = await selected.storage.get_portfolio_history(limit=limit, agent=selected.agent)
        latest = history[0] if history else None
        return portfolio_chart(history, capital=await _starting_capital(selected, latest))

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:  # pragma: no cover - trivial
        return {"ok": True}

    return app
