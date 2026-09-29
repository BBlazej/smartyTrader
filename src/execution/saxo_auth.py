"""Saxo OpenAPI OAuth — authorization-code grant with rotating refresh tokens (§7.66 step 4).

The 24 h developer token only suits a manual ``--once``; unattended runs need an OAuth
app. Contract (Saxo developer portal, "Authorization Code Grant", checked 2026-09-29):

* ``GET {auth}/authorize?response_type=code&client_id=<AppKey>&redirect_uri=<AppUrl>&state=…``
  — the user logs in once in a browser (``scripts/saxo_login.py``);
* ``POST {auth}/token`` with HTTP Basic ``AppKey:AppSecret`` and
  ``grant_type=authorization_code&code=…&redirect_uri=…`` →
  ``{access_token, expires_in (1200), token_type: Bearer, refresh_token,
  refresh_token_expires_in (2400)}``;
* ``POST {auth}/token`` with ``grant_type=refresh_token&refresh_token=…&redirect_uri=…``
  → a new pair; the refresh token **rotates** (the old one is spent).

``{auth}`` is ``https://sim.logonvalidation.net`` (SIM, documented) or
``https://live.logonvalidation.net`` (LIVE — not shown in the docs' examples, so it is
configurable: ``saxo_execution.oauth.auth_base_url``).

On SIM a refresh token lives only 40 minutes, while the stocks agent makes no API calls
outside market hours — so :class:`SaxoOAuth` keeps the session alive with a background
refresh every ``keepalive_minutes``. Tokens persist in a ``0600`` JSON file (rotation
must survive a restart) and never appear in logs or error messages; the app secret comes
from the environment only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

logger = structlog.get_logger()

AUTH_BASE_URLS: dict[str, str] = {
    "sim": "https://sim.logonvalidation.net",
    "live": "https://live.logonvalidation.net",
}
#: Refresh-token lifetime assumed when a response omits ``refresh_token_expires_in``.
DEFAULT_REFRESH_TTL = 2400


class SaxoAuthError(RuntimeError):
    """OAuth failed; the message never contains a token or the app secret."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "access_expires_at": self.access_expires_at.isoformat(),
                "refresh_token": self.refresh_token,
                "refresh_expires_at": self.refresh_expires_at.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> TokenSet:
        data = json.loads(raw)
        return cls(
            access_token=str(data["access_token"]),
            access_expires_at=datetime.fromisoformat(data["access_expires_at"]),
            refresh_token=str(data["refresh_token"]),
            refresh_expires_at=datetime.fromisoformat(data["refresh_expires_at"]),
        )


class TokenStore:
    """The token pair on disk: owner-only permissions, atomic replace."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> TokenSet | None:
        try:
            return TokenSet.from_json(self.path.read_text())
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError) as exc:
            raise SaxoAuthError(
                f"token file {self.path} is unreadable: {type(exc).__name__}"
            ) from exc

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(tokens.to_json())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)


class SaxoOAuth:
    """Hands out a valid access token, refreshing (and persisting) as needed."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        redirect_uri: str,
        store: TokenStore,
        *,
        environment: str = "sim",
        auth_base_url: str | None = None,
        refresh_margin_seconds: float = 120.0,
        keepalive_minutes: float = 10.0,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if environment not in AUTH_BASE_URLS:
            raise ValueError("Saxo environment must be 'sim' or 'live'")
        if not app_key or not app_secret:
            raise ValueError("a Saxo app key and app secret are required for OAuth")
        self.environment = environment
        self.auth_base_url = (auth_base_url or AUTH_BASE_URLS[environment]).rstrip("/")
        self.redirect_uri = redirect_uri
        self._app_key = app_key
        self._store = store
        self._margin = timedelta(seconds=refresh_margin_seconds)
        self._keepalive = keepalive_minutes * 60.0
        self._now = clock or (lambda: datetime.now(UTC))
        self._http = httpx.AsyncClient(
            auth=(app_key, app_secret), timeout=timeout_seconds, transport=transport
        )
        self._lock = asyncio.Lock()
        self._tokens: TokenSet | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

    # ── Login (scripts/saxo_login.py) ─────────────────────

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._app_key,
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        return f"{self.auth_base_url}/authorize?{query}"

    async def exchange_code(self, code: str) -> TokenSet:
        tokens = await self._token_request({"grant_type": "authorization_code", "code": code})
        self._tokens = tokens
        self._store.save(tokens)
        logger.info(
            "saxo login stored",
            environment=self.environment,
            access_expires_at=tokens.access_expires_at.isoformat(),
            refresh_expires_at=tokens.refresh_expires_at.isoformat(),
        )
        return tokens

    # ── Runtime ───────────────────────────────────────────

    async def access_token(self) -> str:
        """A token valid for at least the refresh margin (refreshing when it is not)."""
        self._ensure_keepalive()
        async with self._lock:
            tokens = self._current()
            if tokens.access_expires_at - self._margin > self._now():
                return tokens.access_token
            return (await self._refresh_locked(tokens)).access_token

    async def invalidate(self) -> None:
        """The gateway rejected the token (401): force a refresh on next use."""
        async with self._lock:
            tokens = self._current()
            self._tokens = TokenSet(
                tokens.access_token,
                self._now() - timedelta(seconds=1),
                tokens.refresh_token,
                tokens.refresh_expires_at,
            )

    async def refresh(self) -> TokenSet:
        async with self._lock:
            return await self._refresh_locked(self._current())

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._keepalive_task
            self._keepalive_task = None
        await self._http.aclose()

    # ── Internals ─────────────────────────────────────────

    def _current(self) -> TokenSet:
        if self._tokens is None:
            self._tokens = self._store.load()
        if self._tokens is None:
            raise SaxoAuthError(
                f"no Saxo login stored at {self._store.path} — run `python -m scripts.saxo_login`"
            )
        return self._tokens

    async def _refresh_locked(self, tokens: TokenSet) -> TokenSet:
        if tokens.refresh_expires_at <= self._now():
            raise SaxoAuthError(
                "the Saxo refresh token has expired — log in again with "
                "`python -m scripts.saxo_login`"
            )
        fresh = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
        )
        self._tokens = fresh
        self._store.save(fresh)  # rotation: the old refresh token is spent now
        logger.info(
            "saxo token refreshed",
            environment=self.environment,
            refresh_expires_at=fresh.refresh_expires_at.isoformat(),
        )
        return fresh

    async def _token_request(self, form: dict[str, str]) -> TokenSet:
        body = {**form, "redirect_uri": self.redirect_uri}
        try:
            response = await self._http.post(f"{self.auth_base_url}/token", data=body)
        except httpx.HTTPError as exc:
            raise SaxoAuthError(f"token request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise SaxoAuthError(
                f"token request rejected: HTTP {response.status_code} ({_oauth_error(response)})"
            )
        try:
            payload = response.json()
            now = self._now()
            return TokenSet(
                access_token=str(payload["access_token"]),
                access_expires_at=now + timedelta(seconds=float(payload["expires_in"])),
                refresh_token=str(payload["refresh_token"]),
                refresh_expires_at=now
                + timedelta(
                    seconds=float(payload.get("refresh_token_expires_in", DEFAULT_REFRESH_TTL))
                ),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise SaxoAuthError(f"malformed token response: {type(exc).__name__}") from exc

    def _ensure_keepalive(self) -> None:
        if self._keepalive <= 0 or self._keepalive_task is not None:
            return
        try:
            self._keepalive_task = asyncio.get_running_loop().create_task(self._keepalive_loop())
        except RuntimeError:  # no running loop (sync callers) — refresh on demand only
            self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """Rotate the pair while the process lives, so quiet hours don't expire the login."""
        while True:
            await asyncio.sleep(self._keepalive)
            try:
                await self.refresh()
            except SaxoAuthError as exc:
                logger.error("saxo session keep-alive failed", error=str(exc))
            except Exception as exc:  # noqa: BLE001 - transient; the next tick retries
                logger.warning("saxo session keep-alive error", error=type(exc).__name__)


def _oauth_error(response: httpx.Response) -> str:
    """``error``/``error_description`` from an OAuth error body (never echoes tokens)."""
    try:
        payload: Any = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(payload, dict):
        parts = [str(payload.get(k)) for k in ("error", "error_description") if payload.get(k)]
        if parts:
            return ": ".join(parts)[:200]
    return response.reason_phrase or "error"
