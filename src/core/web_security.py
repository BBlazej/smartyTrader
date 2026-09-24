"""Browser-facing request guards for the dashboard and control API (§7.43).

Both apps are unauthenticated and bound to loopback — but loopback is reachable by
the operator's *own browser*, so any website open in it could otherwise drive them:

* **Cross-site requests (CSRF).** A plain ``<form method=POST>`` on a foreign page is
  a CORS "simple request" — no preflight — so it could pause agents, latch close-all
  or rewrite risk overrides. :class:`OriginGuardMiddleware` rejects state-changing
  requests whose ``Origin`` (or, absent that, ``Referer``) is not an allowed host;
  the dashboard additionally checks a per-process CSRF token on every write.
* **DNS rebinding.** An attacker domain re-resolved to 127.0.0.1 makes the browser
  treat the app as same-origin; only the ``Host`` header still names the attacker.
  Starlette's ``TrustedHostMiddleware`` rejects any request whose ``Host`` is not in
  the allowlist — reads included.

Non-browser clients (curl, scripts) send neither ``Origin`` nor ``Referer`` and are
unaffected by the origin check; they are not a cross-site vector.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

#: Always-allowed loopback names (the apps bind to loopback by default).
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "[::1]")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})

CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"


def allowed_hosts(bind_host: str | None, extra: Iterable[str] | None = None) -> list[str]:
    """Loopback names + the configured bind host (unless a wildcard) + ``extra``."""
    hosts = list(LOOPBACK_HOSTS)
    if bind_host and bind_host not in _WILDCARD_BINDS and bind_host not in hosts:
        hosts.append(bind_host)
    for host in extra or ():
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _hostname(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return parts.hostname


class OriginGuardMiddleware:
    """Reject state-changing requests carrying a foreign ``Origin``/``Referer`` (403)."""

    def __init__(self, app: ASGIApp, hosts: Iterable[str]) -> None:
        self.app = app
        # urlsplit().hostname strips IPv6 brackets — normalize the allowlist the same way.
        self.hosts = {h.strip("[]").lower() for h in hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in _SAFE_METHODS:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]
            }
            source = headers.get("origin") or headers.get("referer")
            if source is not None:
                host = _hostname(source)
                if source == "null" or host is None or host.lower() not in self.hosts:
                    response = PlainTextResponse(
                        "cross-site request rejected (origin not allowed)", status_code=403
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def install_request_guards(app: FastAPI, hosts: list[str]) -> None:
    """Host allowlist (DNS rebinding) + origin check (CSRF) on ``app``."""
    app.add_middleware(OriginGuardMiddleware, hosts=hosts)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)


def new_csrf_token() -> str:
    """Per-process synchronizer token: embedded in the dashboard's pages, which a
    cross-site page cannot read (same-origin policy; rebinding blocked by Host)."""
    return secrets.token_urlsafe(32)


def csrf_ok(expected: str, supplied: str | None) -> bool:
    return bool(supplied) and secrets.compare_digest(expected, supplied)
