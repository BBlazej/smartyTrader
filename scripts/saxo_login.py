"""One-time Saxo OpenAPI login for unattended stocks runs (§7.66 step 4).

Stores the OAuth token pair the stocks runner then keeps refreshing on its own.
Needs ``saxo_execution.oauth`` in ``config/settings.yaml`` and the app credentials
in ``.env`` (``SAXO_APP_KEY`` / ``SAXO_APP_SECRET`` — from your app on the Saxo
developer portal, whose redirect URL must equal ``saxo_execution.oauth.redirect_uri``).

    python -m scripts.saxo_login            # opens the browser, catches the redirect locally
    python -m scripts.saxo_login --paste    # paste the redirected URL instead

The script prints only expiry times — never a token. Re-run it whenever the runner
logs that the refresh token has expired (e.g. after the PC was off longer than the
refresh-token lifetime: 40 minutes on SIM).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import webbrowser
from urllib.parse import parse_qs, urlparse

from src.core.config import Settings
from src.core.runner import load_dotenv
from src.execution.saxo_auth import SaxoAuthError, SaxoOAuth, TokenStore

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def parse_callback(url: str, expected_state: str) -> str:
    """The ``code`` from a redirect URL, after checking ``state`` (CSRF) and errors."""
    query = parse_qs(urlparse(url).query)
    if "error" in query:
        raise SaxoAuthError(f"login refused: {query['error'][0]}")
    if query.get("state", [None])[0] != expected_state:
        raise SaxoAuthError("state mismatch — the redirect is not from this login attempt")
    code = query.get("code", [None])[0]
    if not code:
        raise SaxoAuthError("the redirect carries no authorization code")
    return code


async def wait_for_redirect(redirect_uri: str, timeout: float = 300.0) -> str:
    """Serve one request on the (local) redirect URI and return its full URL."""
    target = urlparse(redirect_uri)
    if target.hostname not in LOCAL_HOSTS or target.scheme != "http":
        raise SaxoAuthError("the redirect URI is not a local http URL — use --paste")
    port = target.port or 80
    received: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode("latin-1").strip()
        parts = request_line.split(" ")
        path = parts[1] if len(parts) >= 2 else "/"
        body = b"Saxo login received - you can close this tab."
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        if urlparse(path).path == (target.path or "/") and not received.done():
            received.set_result(f"http://{target.hostname}:{port}{path}")

    server = await asyncio.start_server(handle, target.hostname, port)
    try:
        return await asyncio.wait_for(received, timeout)
    finally:
        server.close()
        await server.wait_closed()


async def login(paste: bool, open_browser: bool) -> int:
    load_dotenv()
    settings = Settings()
    cfg = settings.saxo_execution
    app_key = os.getenv("SAXO_APP_KEY", "").strip()
    app_secret = os.getenv("SAXO_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        print("SAXO_APP_KEY and SAXO_APP_SECRET must be set in .env", file=sys.stderr)
        return 1
    store = TokenStore(cfg.oauth.token_path(settings.storage.data_dir, cfg.environment))
    oauth = SaxoOAuth(
        app_key,
        app_secret,
        cfg.oauth.redirect_uri,
        store,
        environment=cfg.environment,
        auth_base_url=cfg.oauth.auth_base_url,
        keepalive_minutes=0,
        timeout_seconds=cfg.request_timeout_seconds,
    )
    state = secrets.token_urlsafe(24)
    url = oauth.authorize_url(state)
    try:
        print(f"Saxo {cfg.environment.upper()} login — open this URL and sign in:\n\n  {url}\n")
        if open_browser:
            webbrowser.open(url)
        if paste:
            redirected = input("Paste the full URL your browser was redirected to: ").strip()
        else:
            print(f"Waiting for the redirect on {cfg.oauth.redirect_uri} (5 min)…")
            redirected = await wait_for_redirect(cfg.oauth.redirect_uri)
        tokens = await oauth.exchange_code(parse_callback(redirected, state))
    except (SaxoAuthError, TimeoutError) as exc:
        print(f"login failed: {exc or 'timed out'}", file=sys.stderr)
        return 1
    finally:
        await oauth.close()
    print(
        f"Stored at {store.path} (mode 0600). Access token valid until "
        f"{tokens.access_expires_at:%Y-%m-%d %H:%M} UTC, refresh token until "
        f"{tokens.refresh_expires_at:%Y-%m-%d %H:%M} UTC — the stocks runner keeps it fresh."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Store a Saxo OpenAPI OAuth login (§7.66).")
    parser.add_argument("--paste", action="store_true", help="paste the redirect URL by hand")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(login(args.paste, not args.no_browser)))


if __name__ == "__main__":
    main()
