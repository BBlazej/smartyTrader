"""Saxo Bank OpenAPI client (§7.66) — the REST seam under :class:`SaxoExecutor`.

Saxo replaces the dead XTB path for stocks (XTB closed its API on 2025-03-14). The
developer **SIM** environment is free (a funded-by-Saxo simulation account) and speaks
exactly the API live accounts use, so paper → SIM → live is a config change, never a
rewrite.

Protocol facts (Saxo developer portal + the maintained ``saxo_openapi`` wrapper docs,
checked 2026-09-27):

* Gateways: SIM ``https://gateway.saxobank.com/sim/openapi``, LIVE
  ``https://gateway.saxobank.com/openapi``. Auth is an OAuth bearer token in the
  ``Authorization`` header — for SIM a 24 h developer token from the portal; unattended
  runs need an OAuth app with refresh tokens (PLAN §7.66 follow-up).
* Accounts ``GET /port/v1/accounts/me`` → ``Data[]`` (``AccountKey``, ``AccountId``,
  ``ClientKey``, ``Currency``, ``Active``); balance ``GET /port/v1/balances``
  (``AccountKey`` + ``ClientKey``) → ``CashBalance``, ``Currency``, ``TotalValue``.
* Instruments ``GET /ref/v1/instruments?Keywords=…&AssetTypes=Stock`` → ``Data[]``
  (``Identifier`` = the Uic, ``Symbol`` like ``AAPL:xnas``, ``CurrencyCode``).
* Orders ``POST /trade/v2/orders`` with ``AccountKey``, ``Uic``, ``AssetType``,
  ``BuySell``, ``Amount`` (base units — shares), ``OrderType: Market``,
  ``OrderDuration.DurationType: DayOrder``, ``ManualOrder: false`` (automated) →
  ``{"OrderId": …}``; errors come back as ``ErrorInfo``. Cancel
  ``DELETE /trade/v2/orders/{id}?AccountKey=…``.
* Fills ``GET /cs/v1/audit/orderactivities?OrderId=…&EntryType=Last`` → the order's
  latest activity: ``Status`` ``Placed`` / ``Fill`` / ``FinalFill`` / ``Cancelled`` …
  with ``FilledAmount`` and ``AveragePrice`` (the venue's fill price, §7.62 rule).
  Filled orders leave ``/port/v1/orders/me``, so the audit log is the fill source.
* Holdings ``GET /port/v1/netpositions/me?FieldGroups=NetPositionBase,NetPositionView``
  → ``NetPositionBase.Amount``/``Uic``/``AssetType``, ``NetPositionView.CurrentPrice``
  — net per instrument whatever the account's position-netting mode.

The token is never logged and never part of an error message.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

SIM_BASE_URL = "https://gateway.saxobank.com/sim/openapi"
LIVE_BASE_URL = "https://gateway.saxobank.com/openapi"


class SaxoApiError(RuntimeError):
    """A Saxo OpenAPI call failed (HTTP error or an ``ErrorInfo`` body)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _error_text(payload: Any) -> str | None:
    """``ErrorCode: Message`` from a Saxo error body (top level or ``ErrorInfo``)."""
    if not isinstance(payload, dict):
        return None
    info = payload.get("ErrorInfo") if isinstance(payload.get("ErrorInfo"), dict) else payload
    code, message = info.get("ErrorCode"), info.get("Message")
    if code is None and message is None:
        return None
    return f"{code or 'Error'}: {message or ''}".strip()


class SaxoClient:
    """Thin async wrapper over the handful of OpenAPI endpoints the executor needs."""

    def __init__(
        self,
        access_token: str,
        *,
        environment: str = "sim",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if environment not in ("sim", "live"):
            raise ValueError("Saxo environment must be 'sim' or 'live'")
        if not access_token:
            raise ValueError("a Saxo access token is required")
        self.environment = environment
        self.base_url = SIM_BASE_URL if environment == "sim" else LIVE_BASE_URL
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise SaxoApiError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = None
        if response.status_code >= 400:
            detail = _error_text(payload) or response.reason_phrase or "error"
            raise SaxoApiError(
                f"{method} {path} → HTTP {response.status_code} ({detail})",
                status_code=response.status_code,
            )
        if isinstance(payload, dict) and isinstance(payload.get("ErrorInfo"), dict):
            raise SaxoApiError(f"{method} {path} rejected ({_error_text(payload)})")
        return payload

    @staticmethod
    def _data(payload: Any) -> list[dict[str, Any]]:
        data = payload.get("Data") if isinstance(payload, dict) else None
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    # ── Portfolio / reference data ────────────────────────

    async def get_accounts(self) -> list[dict[str, Any]]:
        return self._data(await self._request("GET", "/port/v1/accounts/me"))

    async def get_balance(self, account_key: str, client_key: str | None = None) -> dict[str, Any]:
        params = {"AccountKey": account_key}
        if client_key:
            params["ClientKey"] = client_key
        payload = await self._request("GET", "/port/v1/balances", params=params)
        return payload if isinstance(payload, dict) else {}

    async def find_instruments(
        self, keyword: str, asset_type: str = "Stock"
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", "/ref/v1/instruments", params={"Keywords": keyword, "AssetTypes": asset_type}
        )
        return self._data(payload)

    async def get_net_positions(self) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            "/port/v1/netpositions/me",
            params={"FieldGroups": "NetPositionBase,NetPositionView"},
        )
        return self._data(payload)

    # ── Trading ───────────────────────────────────────────

    async def place_market_order(
        self,
        *,
        account_key: str,
        uic: int,
        buy_sell: str,
        amount: float,
        asset_type: str = "Stock",
    ) -> str:
        """Place a day market order; returns Saxo's ``OrderId``."""
        body = {
            "AccountKey": account_key,
            "Uic": uic,
            "AssetType": asset_type,
            "BuySell": buy_sell,
            "Amount": amount,
            "OrderType": "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            # Automated (agent) order — Saxo requires the flag on every placement.
            "ManualOrder": False,
        }
        payload = await self._request("POST", "/trade/v2/orders", json=body)
        order_id = payload.get("OrderId") if isinstance(payload, dict) else None
        if order_id in (None, ""):
            raise SaxoApiError("order placement returned no OrderId")
        return str(order_id)

    async def cancel_order(self, order_id: str, account_key: str) -> None:
        await self._request(
            "DELETE", f"/trade/v2/orders/{order_id}", params={"AccountKey": account_key}
        )

    async def get_order_activity(self, order_id: str) -> dict[str, Any] | None:
        """The order's latest audit entry (``None`` while the log has none yet)."""
        payload = await self._request(
            "GET", "/cs/v1/audit/orderactivities", params={"OrderId": order_id, "EntryType": "Last"}
        )
        rows = self._data(payload)
        return rows[-1] if rows else None
