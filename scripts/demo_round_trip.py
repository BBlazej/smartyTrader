"""Controlled BUY → SELL round trip on the OKX **demo** account (§7.28) — demo-only CLI.

An overnight keyed demo run can end with zero orders (the LLM held every bar), which
leaves the venue half of §7.28 — order format, fills, reconciliation, fee reporting —
unverified. This script forces one small round trip through the *same* keyed
``CcxtExecutor`` the runner builds (``scripts.run_crypto_agent`` wiring, incl. the
``venue_orders`` policy), handing it the reference price the pipeline hands it (the
live snapshot's last close) — the executor turns that into the venue order (§7.75)::

    python -m scripts.demo_round_trip                      # dry run: balances, prices, limits
    python -m scripts.demo_round_trip --yes                # BUY ~€20 of BTC/EUR, then SELL it
    python -m scripts.demo_round_trip --symbol ETH/EUR --notional 30 --yes

The first run (2026-09-28) sent a limit at the bare close; it rested under the ask and
was cancelled — the finds are PLAN §7.75. The report now shows the order terms the
executor actually sent, the venue's fee payloads and this account's fee rates.

Safety: refuses unless the runner's own mode selection yields ``<exchange>-sandbox``
*and* the ccxt client really is in sandbox mode — it can never reach a live account.
It holds the crypto runner lock (§7.52), so the agent cannot trade the same demo
account concurrently, and it writes **nothing** to the database — no order rows, no
closing fills, so the agent's loss streak/ledger are unaffected. An order still
resting after ``--timeout`` is cancelled; if the SELL rests, the position is then
closed with a market order so the demo account is left as found.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import structlog

from scripts.run_crypto_agent import _build_data_and_execution
from src.core import db_layout
from src.core.config import Settings
from src.core.models import OrderResult, OrderSide
from src.core.runner import RunnerLock, load_dotenv
from src.execution.ccxt_executor import CcxtExecutor
from src.monitoring import setup_logging

log = structlog.get_logger().bind(component="demo_round_trip")

#: Sanity ceiling on the forced notional (quote currency) — this is a smoke test.
MAX_NOTIONAL = 200.0


class RawRecorder:
    """Records the raw ccxt payloads of order calls on the executor's client.

    ``CcxtExecutor`` deliberately reduces payloads to an ``OrderResult``; the smoke
    test also wants what the venue actually sent back (status, fees, average).
    Wraps the client's bound methods in place, so the executor is untouched.
    """

    def __init__(self, client: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        for name in ("create_order", "fetch_order", "cancel_order"):
            method = getattr(client, name, None)
            if callable(method):
                setattr(client, name, self._wrap(name, method))

    def _wrap(self, name: str, method: Any) -> Any:
        async def recorded(*args: Any, **kwargs: Any) -> Any:
            result = await method(*args, **kwargs)
            self.calls.append(
                {
                    "call": name,
                    "args": [str(a) for a in args],
                    "kwargs": {k: str(v) for k, v in kwargs.items()},
                    "raw": result,
                }
            )
            return result

        return recorded

    def sent(self) -> dict[str, Any] | None:
        """``{type, side, amount, price}`` of the latest ``create_order`` call."""
        for entry in reversed(self.calls):
            if entry["call"] == "create_order":
                args = entry["args"]
                return {
                    "type": args[1] if len(args) > 1 else None,
                    "side": args[2] if len(args) > 2 else None,
                    "amount": args[3] if len(args) > 3 else None,
                    "price": entry["kwargs"].get("price"),
                }
        return None

    def last(self, name: str, order_id: str | None = None) -> dict[str, Any] | None:
        for entry in reversed(self.calls):
            raw = entry["raw"] or {}
            if entry["call"] == name and (order_id is None or str(raw.get("id")) == order_id):
                return raw
        return None


def ensure_demo(mode: str, client: Any) -> None:
    """Refuse anything that is not a sandbox/demo client — never a live account."""
    if not mode.endswith("-sandbox"):
        raise SystemExit(
            f"refusing: executor mode is '{mode}', not '<exchange>-sandbox'. Needs "
            "EXCHANGE_API_KEY/_SECRET/_PASSPHRASE in .env and crypto_agent.testnet: true."
        )
    if not getattr(client, "isSandboxModeEnabled", False):
        raise SystemExit("refusing: the keyed ccxt client is not in sandbox mode")


async def settle(
    executor: CcxtExecutor, order: OrderResult, *, timeout: float, poll: float
) -> OrderResult:
    """Drive a ``pending`` order to a terminal status through the executor's reconcile path.

    Uses ``reconcile_open_orders`` + ``confirm_reconciled`` exactly like the agent does
    once per cycle — just polled every ``poll`` seconds. Still open at ``timeout`` →
    cancelled (the returned result stays ``pending`` with a reason).
    """
    if order.status != "pending":
        return order
    waited = 0.0
    while waited < timeout:
        await asyncio.sleep(poll)
        waited += poll
        for result in await executor.reconcile_open_orders():
            if result.order_id == order.order_id:
                executor.confirm_reconciled(result.order_id)
                log.info("order reconciled", order_id=result.order_id, status=result.status)
                return result
    cancelled = await executor.cancel_order(order.order_id)
    log.warning("order still resting at timeout", order_id=order.order_id, cancelled=cancelled)
    return order.model_copy(update={"reason": f"resting after {timeout:.0f}s; cancel={cancelled}"})


def _base_total(balance: dict[str, Any], symbol: str) -> float:
    return float((balance.get("total") or {}).get(symbol.split("/")[0]) or 0.0)


def _fees(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    fees = raw.get("fees")
    if isinstance(fees, list) and fees:
        return [{"cost": f.get("cost"), "currency": f.get("currency")} for f in fees if f]
    fee = raw.get("fee")
    return [{"cost": fee.get("cost"), "currency": fee.get("currency")}] if fee else []


def _summary(order: OrderResult, raw: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "status": order.status,
        "quantity": order.quantity,
        "price": order.price,
        "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        "realized_pnl": order.realized_pnl,
        "closed_entries": [e.model_dump() for e in order.closed_entries],
        "reason": order.reason,
        "venue_status": (raw or {}).get("status"),
        "venue_average": (raw or {}).get("average"),
        "venue_filled": (raw or {}).get("filled"),
        "venue_fees": _fees(raw),
    }


async def round_trip(
    executor: CcxtExecutor,
    provider: Any,
    recorder: RawRecorder,
    *,
    symbol: str,
    notional: float,
    timeframe: str,
    timeout: float,
    poll: float,
    execute: bool,
) -> dict[str, Any]:
    """One BUY of ~``notional`` then a SELL of the whole ledger position; returns a report."""
    client = executor.client
    markets = await client.load_markets()  # type: ignore[attr-defined]
    if symbol not in await executor.tradable_symbols():
        raise SystemExit(f"{symbol} is not tradable on this demo account")
    market = markets[symbol]
    limits = market.get("limits") or {}
    min_amount = float((limits.get("amount") or {}).get("min") or 0.0)
    min_cost = float((limits.get("cost") or {}).get("min") or 0.0)

    snapshot = await provider.fetch_snapshot(symbol, timeframe)
    if not snapshot.candles:
        raise SystemExit(f"no live candles for {symbol} — no price, no trade (§7.55)")
    live_price = snapshot.candles[-1].close  # what the pipeline prices orders at
    ticker = await client.fetch_ticker(symbol)  # type: ignore[attr-defined]
    quantity = float(client.amount_to_precision(symbol, notional / live_price))  # type: ignore[attr-defined]

    report: dict[str, Any] = {
        "symbol": symbol,
        "venue": executor.venue,
        "live_last_close": live_price,
        "demo_bid": ticker.get("bid"),
        "demo_ask": ticker.get("ask"),
        "demo_last": ticker.get("last"),
        "min_amount": min_amount,
        "min_cost": min_cost,
        "buy_quantity": quantity,
        "notional": quantity * live_price,
    }
    if quantity <= 0 or quantity < min_amount or quantity * live_price < min_cost:
        raise SystemExit(
            f"€{notional} is below the venue minimum (amount ≥ {min_amount}, cost ≥ {min_cost})"
            " — raise --notional"
        )

    report["venue_fee_rates"] = await executor.trading_fee(symbol)
    cash_before = await executor.get_cash()
    balance_before = await client.fetch_balance()  # type: ignore[attr-defined]
    report["cash_before"] = cash_before
    report["base_before"] = _base_total(balance_before, symbol)
    if not execute:
        report["dry_run"] = True
        return report
    if cash_before < quantity * live_price * 1.01:
        raise SystemExit(f"demo cash {cash_before} too low for the BUY")

    executor.update_price(symbol, live_price)  # the pipeline marks before it trades

    # ── BUY: marketable limit at the live close, exactly as the pipeline sends it.
    buy = await executor.place_order(symbol, OrderSide.BUY, quantity, price=live_price)
    report["buy_sent"] = recorder.sent()
    report["buy_initial_status"] = buy.status
    buy = await settle(executor, buy, timeout=timeout, poll=poll)
    report["buy"] = _summary(
        buy, recorder.last("fetch_order", buy.order_id) or recorder.last("create_order")
    )
    positions = {p.symbol: p for p in await executor.get_positions()}
    held = positions[symbol].quantity if symbol in positions else 0.0
    report["ledger_after_buy"] = held
    report["base_delta_after_buy"] = (
        _base_total(await client.fetch_balance(), symbol) - report["base_before"]  # type: ignore[attr-defined]
    )
    if buy.status != "filled" or held <= 0:
        report["outcome"] = "BUY did not fill — nothing to sell"
        return report

    # ── SELL: the whole ledger position (a SELL closes in full, §7.47), same pricing.
    sell_price = (await provider.fetch_snapshot(symbol, timeframe)).candles[-1].close
    executor.update_price(symbol, sell_price)
    sell = await executor.place_order(symbol, OrderSide.SELL, held, price=sell_price)
    report["sell_sent"] = recorder.sent()
    report["sell_initial_status"] = sell.status
    sell = await settle(executor, sell, timeout=timeout, poll=poll)
    if sell.status == "pending":
        # Resting limit was cancelled — flatten with a market order so the account
        # is left as found (also exercises the executor's market-order path).
        log.warning("SELL rested; closing with a market order", symbol=symbol)
        report["sell_limit"] = _summary(sell, recorder.last("create_order"))
        sell = await executor.place_order(symbol, OrderSide.SELL, held)
        sell = await settle(executor, sell, timeout=timeout, poll=poll)
    report["sell"] = _summary(
        sell, recorder.last("fetch_order", sell.order_id) or recorder.last("create_order")
    )

    report["ledger_after_sell"] = sum(
        p.quantity for p in await executor.get_positions() if p.symbol == symbol
    )
    report["cash_after"] = await executor.get_cash()
    report["cash_delta"] = report["cash_after"] - cash_before
    report["base_delta_total"] = (
        _base_total(await client.fetch_balance(), symbol) - report["base_before"]  # type: ignore[attr-defined]
    )
    report["outcome"] = (
        "round trip complete" if sell.status == "filled" else "SELL did not fill — CHECK THE DEMO"
    )
    return report


def _print_report(report: dict[str, Any]) -> None:
    print("\n=== OKX demo round trip (§7.28) ===")
    print(json.dumps(report, indent=2, default=str))


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    settings = Settings()
    setup_logging(settings.monitoring.log_level)
    if not 0 < args.notional <= MAX_NOTIONAL:
        raise SystemExit(f"--notional must be in (0, {MAX_NOTIONAL:.0f}]")
    symbol = args.symbol
    quote = settings.crypto_agent.quote_currency
    if not symbol.endswith(f"/{quote}"):
        raise SystemExit(f"{symbol} is not quoted in crypto_agent.quote_currency ({quote})")

    # The demo book's runner lock (§7.78): the script is demo-only, so a concurrent
    # paper/real crypto runner is not competing for this account and is not blocked.
    lock = RunnerLock(db_layout.lock_path(settings.storage.data_dir, db_layout.DEMO, "crypto"))
    if not lock.acquire():
        raise SystemExit(
            "the demo crypto runner is running — stop it first (it trades this account)"
        )
    provider, executor, mode = _build_data_and_execution(settings)
    try:
        if not isinstance(executor, CcxtExecutor):
            raise SystemExit(f"refusing: executor mode is '{mode}' (no keyed demo executor)")
        ensure_demo(mode, executor.client)
        recorder = RawRecorder(executor.client)
        report = await round_trip(
            executor,
            provider,
            recorder,
            symbol=symbol,
            notional=args.notional,
            timeframe=settings.crypto_agent.timeframe or "1h",
            timeout=args.timeout,
            poll=args.poll,
            execute=args.yes,
        )
        report["paper_fee_pct"] = settings.execution.paper_cost_params("crypto")["paper_fee_pct"]
        if args.raw:
            report["raw_calls"] = recorder.calls
        _print_report(report)
        if not args.yes:
            print("\ndry run — nothing was placed. Re-run with --yes to trade on the demo.")
            return 0
        return 0 if report.get("outcome") == "round trip complete" else 1
    finally:
        await executor.close()
        await provider.close()
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Force one small BUY → SELL round trip on the OKX demo account (§7.28)."
    )
    parser.add_argument("--symbol", default="BTC/EUR", help="demo-tradable pair (default BTC/EUR)")
    parser.add_argument(
        "--notional", type=float, default=20.0, help="BUY size in quote currency (default 20)"
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="seconds to wait for a resting order"
    )
    parser.add_argument("--poll", type=float, default=3.0, help="reconcile poll interval (s)")
    parser.add_argument("--raw", action="store_true", help="include raw ccxt payloads")
    parser.add_argument("--yes", action="store_true", help="actually place the demo orders")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
