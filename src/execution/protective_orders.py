"""Venue-side protective orders (§7.34): one OKX OCO per open spot position.

The pipeline's SL/TP checks (§7.9) run only while the agent runs, and only when a
cycle reaches the symbol. This keeps a matching order *at the venue*: an OKX algo
order of type ``oco`` (stop-loss + take-profit, or ``conditional`` when only one
level is set). It sells the whole position at market when either trigger price is
hit, even while the agent is down. The local checks stay — whichever fires first
closes the position, and the other side is cancelled or no longer has anything to sell.

This class only talks to the venue: place, cancel, poll and adopt-at-startup. The
executor owns the ledger and books a triggered order's fill like any other closing
fill (:meth:`CcxtExecutor._book_triggered`).

OKX facts, verified on the demo (2026-10-01):

* ccxt routes the request to the algo endpoint only with ``type="oco"`` (or
  ``"conditional"``), and needs ``tdMode="cash"`` — its default margin mode is refused
  on a spot-only account (51010).
* While the OCO lives, the coins are frozen ("used"), so every other SELL must
  cancel it first.
* Once it fires, the algo order's ``state`` is ``effective``, ``ordId`` names the
  child market order, and ``actualSide`` says which side fired (``sl``/``tp``).
  Cancelling it then raises ``OrderNotFound`` (51400).
* Ours are tagged with an ``algoClOrdId`` prefix, so a restart finds them in the open
  and history lists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog

logger = structlog.get_logger()

#: ``algoClOrdId`` prefix of this agent's protective orders (OKX: ≤ 32 alphanumerics).
CLIENT_ID_PREFIX = "taprot"
_ALGO = {"trigger": True}
#: The algo order types we place — listed one by one (ccxt defaults to ``trigger``).
_KINDS = ("oco", "conditional")
#: Algo states after which the order no longer protects anything without having sold.
_DEAD_STATES = {"canceled", "order_failed"}


@dataclass
class ProtectiveOrder:
    """A live protective order at the venue."""

    algo_id: str
    symbol: str
    quantity: float
    stop_loss: float | None
    take_profit: float | None

    def matches(self, quantity: float, stop_loss: float | None, take_profit: float | None) -> bool:
        return (
            abs(self.quantity - quantity) <= 1e-12
            and self.stop_loss == stop_loss
            and self.take_profit == take_profit
        )


@dataclass
class Triggered:
    """A protective order that fired at the venue; ``child`` is its filled market order."""

    symbol: str
    algo_id: str
    child_id: str
    side_hit: str  # "sl" | "tp" | "" (unknown)
    child: dict[str, Any]

    @property
    def reason(self) -> str:
        return {"sl": "venue stop-loss", "tp": "venue take-profit"}.get(
            self.side_hit, "venue protective order"
        )


def _is_ours(raw: dict[str, Any]) -> bool:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    return str(info.get("algoClOrdId") or raw.get("clientOrderId") or "").startswith(
        CLIENT_ID_PREFIX
    )


def _algo_state(raw: dict[str, Any]) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    return str(info.get("state") or "")


def _child_id(raw: dict[str, Any]) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    child = info.get("ordId") or next(iter(info.get("ordIdList") or []), "")
    return str(child or "")


class ProtectiveOrders:
    """The venue side of §7.34 for one ccxt client."""

    def __init__(self, client: Any, price_to_precision: Callable[[str, float], float]) -> None:
        self._client = client
        self._price = price_to_precision
        self._live: dict[str, ProtectiveOrder] = {}
        # Symbols whose last placement failed — logged once until one succeeds.
        self._failing: set[str] = set()

    def get(self, symbol: str) -> ProtectiveOrder | None:
        return self._live.get(symbol)

    def symbols(self) -> list[str]:
        return list(self._live)

    async def place(
        self, symbol: str, quantity: float, stop_loss: float | None, take_profit: float | None
    ) -> ProtectiveOrder | None:
        """Place an OCO (or a one-sided conditional) selling ``quantity`` at market."""
        params: dict[str, Any] = {
            "tdMode": "cash",
            "algoClOrdId": f"{CLIENT_ID_PREFIX}{uuid4().hex[:24]}",
        }
        if stop_loss is not None:
            params["stopLossPrice"] = self._price(symbol, stop_loss)
        if take_profit is not None:
            params["takeProfitPrice"] = self._price(symbol, take_profit)
        kind = "oco" if stop_loss is not None and take_profit is not None else "conditional"
        try:
            raw = await self._client.create_order(symbol, kind, "sell", quantity, None, params)
        except Exception as exc:  # noqa: BLE001 - the local SL/TP check still protects
            if symbol not in self._failing:
                self._failing.add(symbol)
                logger.warning(
                    "protective order not placed; local SL/TP checks only",
                    symbol=symbol,
                    quantity=quantity,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    error=str(exc),
                )
            return None
        self._failing.discard(symbol)
        order = ProtectiveOrder(
            algo_id=str((raw or {}).get("id") or ""),
            symbol=symbol,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        self._live[symbol] = order
        logger.info(
            "protective order placed at the venue",
            symbol=symbol,
            algo_id=order.algo_id,
            kind=kind,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return order

    async def cancel(self, symbol: str) -> Triggered | None:
        """Withdraw ``symbol``'s protective order before anything else sells the coins.

        Returns the :class:`Triggered` fill when it had already fired, so the caller
        can book it before deciding what is left to sell. A failure other than "already
        gone" is logged and the order is still treated as gone locally. The next poll
        sees it again if it is still live.
        """
        order = self._live.pop(symbol, None)
        if order is None:
            return None
        try:
            await self._client.cancel_order(order.algo_id, symbol, _ALGO)
            logger.info("protective order cancelled", symbol=symbol, algo_id=order.algo_id)
            return None
        except Exception as exc:  # noqa: BLE001 - fired, or the venue hiccuped: look
            logger.info(
                "protective order cancel refused; checking whether it fired",
                symbol=symbol,
                algo_id=order.algo_id,
                error=str(exc),
            )
        triggered = await self._check(order)
        if triggered is None and order.symbol not in self._live:
            # Not fired and not cancelled as far as we can tell: keep watching it.
            state = await self._state(order)
            if state and state not in _DEAD_STATES and state != "effective":
                self._live[symbol] = order
        return triggered

    async def poll(self) -> list[Triggered]:
        """One status read per live order: report fills, forget dead ones."""
        fired: list[Triggered] = []
        for symbol, order in list(self._live.items()):
            state = await self._state(order)
            if state == "effective":
                triggered = await self._check(order)
                if triggered is not None:
                    self._live.pop(symbol, None)
                    fired.append(triggered)
            elif state in _DEAD_STATES:
                self._live.pop(symbol, None)
                logger.warning(
                    "protective order ended without selling; it will be re-placed",
                    symbol=symbol,
                    algo_id=order.algo_id,
                    state=state,
                )
        return fired

    async def adopt(self, symbols: list[str], known_order_ids: set[str]) -> list[Triggered]:
        """Startup (§7.58): find what our protective orders did while we were down.

        Per tracked symbol: fired orders whose child fill is not already in storage are
        returned for booking. Still-live ones are cancelled, because the caller places
        fresh ones that match the rebuilt ledger exactly.
        """
        fired: list[Triggered] = []
        for symbol in symbols:
            for raw in await self._listed(symbol, "fetch_closed_orders", 100):
                if not _is_ours(raw) or _algo_state(raw) != "effective":
                    continue
                child_id = _child_id(raw)
                if not child_id or child_id in known_order_ids:
                    continue
                order = ProtectiveOrder(str(raw.get("id") or ""), symbol, 0.0, None, None)
                triggered = await self._check(order, raw)
                if triggered is not None:
                    fired.append(triggered)
            for raw in await self._listed(symbol, "fetch_open_orders", None):
                if not _is_ours(raw):
                    continue
                try:
                    await self._client.cancel_order(str(raw.get("id")), symbol, _ALGO)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "stale protective order not cancelled",
                        symbol=symbol,
                        algo_id=raw.get("id"),
                        error=str(exc),
                    )
        return fired

    async def _listed(self, symbol: str, method: str, limit: int | None) -> list[dict[str, Any]]:
        """Our algo orders of both kinds. ccxt asks OKX for plain ``trigger`` orders
        unless ``ordType`` is given, which silently hid every OCO (found on the demo)."""
        rows: list[dict[str, Any]] = []
        for kind in _KINDS:
            try:
                listed = await getattr(self._client, method)(
                    symbol, None, limit, {**_ALGO, "ordType": kind}
                )
            except Exception as exc:  # noqa: BLE001 - a restart must not die on it
                logger.warning(
                    "protective order list unavailable", symbol=symbol, kind=kind, error=str(exc)
                )
                continue
            rows.extend(r for r in listed or [] if isinstance(r, dict))
        return rows

    async def _state(self, order: ProtectiveOrder) -> str:
        try:
            raw = await self._client.fetch_order(order.algo_id, order.symbol, _ALGO)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            logger.warning("protective order poll failed", symbol=order.symbol, error=str(exc))
            return ""
        return _algo_state(raw or {})

    async def _check(
        self, order: ProtectiveOrder, algo: dict[str, Any] | None = None
    ) -> Triggered | None:
        """The filled child order of a fired protective order, else ``None``."""
        try:
            if algo is None:
                algo = await self._client.fetch_order(order.algo_id, order.symbol, _ALGO)
            if _algo_state(algo or {}) != "effective":
                return None
            child_id = _child_id(algo)
            if not child_id:
                return None
            child = await self._client.fetch_order(child_id, order.symbol)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            logger.warning("protective order check failed", symbol=order.symbol, error=str(exc))
            return None
        if not isinstance(child, dict) or not float(child.get("filled") or 0):
            return None  # the market child is still resolving — next poll
        info = algo.get("info") if isinstance(algo.get("info"), dict) else {}
        return Triggered(
            symbol=order.symbol,
            algo_id=order.algo_id,
            child_id=child_id,
            side_hit=str(info.get("actualSide") or ""),
            child=child,
        )
