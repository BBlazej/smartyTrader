"""Saxo OAuth: authorization-code grant, rotating refresh tokens, keep-alive (§7.66 step 4)."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from scripts.saxo_login import parse_callback, wait_for_redirect
from src.core.config import SaxoExecutionSettings
from src.execution.saxo_auth import SaxoAuthError, SaxoOAuth, TokenSet, TokenStore
from src.execution.saxo_client import SaxoApiError, SaxoClient

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SECRET = "app-secret-XYZ"


class FakeTokenServer:
    """``/token`` endpoint: issues numbered pairs, rotates refresh tokens."""

    def __init__(self) -> None:
        self.requests: list[dict[str, list[str]]] = []
        self.auth_headers: list[str] = []
        self.issued = 0
        self.valid_refresh: set[str] = set()
        self.fail_with: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        form = parse_qs(request.content.decode())
        self.requests.append(form)
        self.auth_headers.append(request.headers.get("authorization", ""))
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": "invalid_grant"})
        if form["grant_type"] == ["refresh_token"]:
            token = form["refresh_token"][0]
            if token not in self.valid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.valid_refresh.discard(token)  # rotation: spent
        self.issued += 1
        refresh = f"refresh-{self.issued}"
        self.valid_refresh.add(refresh)
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{self.issued}",
                "expires_in": 1200,
                "token_type": "Bearer",
                "refresh_token": refresh,
                "refresh_token_expires_in": 2400,
            },
        )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def make(tmp_path: Path, server: FakeTokenServer, clock: Clock, **kwargs) -> SaxoOAuth:
    return SaxoOAuth(
        "app-key",
        SECRET,
        "http://localhost:8765/callback",
        TokenStore(tmp_path / "saxo_sim.token.json"),
        transport=httpx.MockTransport(server),
        clock=clock,
        keepalive_minutes=kwargs.pop("keepalive_minutes", 0),
        **kwargs,
    )


def test_authorize_url() -> None:
    oauth = SaxoOAuth("app-key", SECRET, "http://localhost:8765/callback", TokenStore("/tmp/x"))
    url = urlparse(oauth.authorize_url("st4te"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://sim.logonvalidation.net/authorize"
    assert parse_qs(url.query) == {
        "response_type": ["code"],
        "client_id": ["app-key"],
        "redirect_uri": ["http://localhost:8765/callback"],
        "state": ["st4te"],
    }
    live = SaxoOAuth("k", "s", "http://x", TokenStore("/tmp/x"), environment="live")
    assert live.auth_base_url == "https://live.logonvalidation.net"


async def test_exchange_code_stores_private_file(tmp_path: Path) -> None:
    server, clock = FakeTokenServer(), Clock()
    oauth = make(tmp_path, server, clock)
    tokens = await oauth.exchange_code("the-code")
    await oauth.close()
    assert server.requests[0] == {
        "grant_type": ["authorization_code"],
        "code": ["the-code"],
        "redirect_uri": ["http://localhost:8765/callback"],
    }
    expected = "Basic " + base64.b64encode(f"app-key:{SECRET}".encode()).decode()
    assert server.auth_headers[0] == expected
    assert tokens.access_expires_at == NOW + timedelta(seconds=1200)
    assert tokens.refresh_expires_at == NOW + timedelta(seconds=2400)
    path = tmp_path / "saxo_sim.token.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert TokenStore(path).load() == tokens


async def test_access_token_cached_then_refreshed_with_rotation(tmp_path: Path) -> None:
    server, clock = FakeTokenServer(), Clock()
    oauth = make(tmp_path, server, clock)
    await oauth.exchange_code("c")
    assert await oauth.access_token() == "access-1"
    clock.now = NOW + timedelta(seconds=1100)  # within the 120 s margin → refresh
    assert await oauth.access_token() == "access-2"
    assert server.requests[-1]["refresh_token"] == ["refresh-1"]
    # The rotated pair is on disk, so a restarted runner continues with refresh-2.
    restarted = make(tmp_path, server, clock)
    clock.now = NOW + timedelta(seconds=2400)
    assert await restarted.access_token() == "access-3"
    await oauth.close()
    await restarted.close()


async def test_invalidate_forces_refresh(tmp_path: Path) -> None:
    server, clock = FakeTokenServer(), Clock()
    oauth = make(tmp_path, server, clock)
    await oauth.exchange_code("c")
    await oauth.invalidate()
    assert await oauth.access_token() == "access-2"
    await oauth.close()


async def test_expired_or_missing_login_says_how_to_fix(tmp_path: Path) -> None:
    server, clock = FakeTokenServer(), Clock()
    oauth = make(tmp_path, server, clock)
    with pytest.raises(SaxoAuthError, match="saxo_login"):
        await oauth.access_token()
    await oauth.exchange_code("c")
    clock.now = NOW + timedelta(hours=2)
    with pytest.raises(SaxoAuthError, match="expired"):
        await oauth.access_token()
    await oauth.close()


async def test_rejected_token_request_never_echoes_secrets(tmp_path: Path) -> None:
    server, clock = FakeTokenServer(), Clock()
    server.fail_with = 401
    oauth = make(tmp_path, server, clock)
    with pytest.raises(SaxoAuthError) as info:
        await oauth.exchange_code("the-code")
    assert "invalid_grant" in str(info.value)
    assert SECRET not in str(info.value) and "the-code" not in str(info.value)
    await oauth.close()


async def test_corrupt_token_file(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text("{not json")
    with pytest.raises(SaxoAuthError, match="unreadable"):
        TokenStore(path).load()


async def test_keepalive_rotates_while_idle(tmp_path: Path) -> None:
    server, clock = FakeTokenServer(), Clock()
    oauth = make(tmp_path, server, clock, keepalive_minutes=0.001)  # 60 ms
    await oauth.exchange_code("c")
    await oauth.access_token()  # starts the keep-alive
    await asyncio.sleep(0.2)
    await oauth.close()
    assert server.issued >= 3


class TestClientWithTokenSource:
    async def test_bearer_per_request_and_401_retry(self, tmp_path: Path) -> None:
        server, clock = FakeTokenServer(), Clock()
        oauth = make(tmp_path, server, clock)
        await oauth.exchange_code("c")
        seen: list[str] = []

        def gateway(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["authorization"])
            if len(seen) == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"Data": [{"AccountKey": "k"}]})

        client = SaxoClient(token_source=oauth, transport=httpx.MockTransport(gateway))
        assert await client.get_accounts() == [{"AccountKey": "k"}]
        assert seen == ["Bearer access-1", "Bearer access-2"]
        await client.close()

    async def test_auth_failure_becomes_api_error(self, tmp_path: Path) -> None:
        server, clock = FakeTokenServer(), Clock()
        oauth = make(tmp_path, server, clock)  # nothing stored
        client = SaxoClient(
            token_source=oauth, transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        with pytest.raises(SaxoApiError, match="not authorized"):
            await client.get_accounts()
        await client.close()

    def test_needs_some_credential(self) -> None:
        with pytest.raises(ValueError):
            SaxoClient()


class TestLoginScript:
    def test_parse_callback(self) -> None:
        url = "http://localhost:8765/callback?code=abc&state=s1"
        assert parse_callback(url, "s1") == "abc"
        with pytest.raises(SaxoAuthError, match="state"):
            parse_callback(url, "other")
        with pytest.raises(SaxoAuthError, match="refused"):
            parse_callback("http://localhost/callback?error=access_denied&state=s1", "s1")

    async def test_local_redirect_listener(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        waiter = asyncio.create_task(
            wait_for_redirect(f"http://127.0.0.1:{port}/callback", timeout=5)
        )
        await asyncio.sleep(0.1)
        async with httpx.AsyncClient() as c:
            response = await c.get(f"http://127.0.0.1:{port}/callback?code=xyz&state=s")
        assert response.status_code == 200
        assert parse_callback(await waiter, "s") == "xyz"

    async def test_non_local_redirect_needs_paste(self) -> None:
        with pytest.raises(SaxoAuthError, match="--paste"):
            await wait_for_redirect("https://example.com/cb")


class TestRunnerWiring:
    def settings(self, tmp_path: Path, oauth: bool) -> SimpleNamespace:
        return SimpleNamespace(
            saxo_execution=SaxoExecutionSettings(
                enabled=True, oauth={"enabled": oauth, "keepalive_minutes": 0}
            ),
            storage=SimpleNamespace(data_dir=str(tmp_path)),
        )

    class Log:
        def __init__(self) -> None:
            self.warnings: list[str] = []

        def warning(self, message: str, **_: object) -> None:
            self.warnings.append(message)

        def info(self, *_: object, **__: object) -> None:
            return None

    async def test_oauth_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.run_stocks_agent import _saxo_client

        log = self.Log()
        monkeypatch.delenv("SAXO_APP_KEY", raising=False)
        assert _saxo_client(self.settings(tmp_path, True), log) is None
        assert "SAXO_APP_KEY" in log.warnings[-1]

        monkeypatch.setenv("SAXO_APP_KEY", "k")
        monkeypatch.setenv("SAXO_APP_SECRET", "s")
        assert _saxo_client(self.settings(tmp_path, True), log) is None
        assert "saxo_login" in log.warnings[-1]

        TokenStore(tmp_path / "saxo_sim.token.json").save(
            TokenSet("a", NOW + timedelta(hours=1), "r", NOW + timedelta(hours=2))
        )
        client = _saxo_client(self.settings(tmp_path, True), log)
        assert client is not None and client._token_source is not None
        await client.close()

    async def test_developer_token_path_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.run_stocks_agent import _saxo_client

        monkeypatch.setenv("SAXO_ACCESS_TOKEN", "dev-token")
        client = _saxo_client(self.settings(tmp_path, False), self.Log())
        assert client is not None and client._token_source is None
        await client.close()
