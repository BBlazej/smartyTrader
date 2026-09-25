"""Backtest entry point — decision replay over stored decisions (§7.14).

Replays the agent's own stored ``llm_decisions`` against fresh historical candles,
through the same risk engine + fee/slippage model as live. Deterministic; **zero LLM
calls** (the LLM replay variant is a separate, later experiment).

Examples:
    python -m scripts.backtest --start 2026-08-01 --end 2026-09-15
    python -m scripts.backtest --days 30 --symbols BTC/USDT ETH/USDT --timeframe 1h
    python -m scripts.backtest --provider yfinance --symbols AAPL --days 90 --report out.json

The candle source is fresh from the venue (the configured exchange's public data via CCXT, or yfinance):
the agent does not run 24/7, so stored ``market_snapshots`` alone are too sparse.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.core.backtester import DecisionReplayBacktester, ReplayDecision
from src.core.config import Settings
from src.core.models import OHLCV
from src.core.storage import Storage


def _parse_dt(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse ``YYYY-MM-DD`` or full ISO datetime into an aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        if end_of_day and len(value) == 10:  # a bare date as the *end* → inclusive day end
            dt = dt + timedelta(days=1) - timedelta(seconds=1)
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _build_history_provider(provider_kind: str, settings: Settings) -> tuple[Any, Any]:
    """Return ``(provider, default_timeframe)`` for the chosen candle source."""
    if provider_kind == "yfinance":
        from src.data.xtb_provider import create_xtb_provider

        return create_xtb_provider(), "1d"
    from src.data.ccxt_provider import create_ccxt_provider

    exchange = settings.crypto_agent.exchange
    if not exchange:
        raise SystemExit("crypto_agent.exchange is not set (e.g. 'myokx')")
    # Public data endpoint: no keys, no sandbox — the backtester never trades.
    return create_ccxt_provider(exchange_id=exchange, testnet=False), "1h"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    setup = structlog.get_logger().bind(component="backtest")

    end = _parse_dt(args.end, end_of_day=True) if args.end else datetime.now(UTC)
    start = _parse_dt(args.start) if args.start else end - timedelta(days=args.days)
    if start >= end:
        raise SystemExit("--start must be before --end")

    storage = Storage(settings.storage.database_path)
    await storage.initialize()
    provider = None
    try:
        # Decisions belong to one agent (§7.39): ccxt candles replay the crypto
        # agent's decisions, yfinance the stocks agent's.
        rows = await storage.get_decisions_in_range(
            start=start,
            end=end,
            symbols=(args.symbols or None),
            agent="stocks" if args.provider == "yfinance" else "crypto",
        )
        decisions = [
            ReplayDecision(
                timestamp=row.timestamp.replace(tzinfo=UTC)
                if row.timestamp.tzinfo is None
                else row.timestamp,
                symbol=row.symbol,
                action=row.action,
                confidence=row.confidence,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
            )
            for row in rows
        ]
        symbols = sorted(args.symbols or []) or sorted({d.symbol for d in decisions})
        if not decisions:
            setup.warning(
                "no stored decisions in range — nothing to replay",
                start=str(start),
                end=str(end),
                note="run the agent first, or widen --days",
            )
        if not symbols:
            await storage.close()
            return {"error": "no symbols to backtest (no decisions and none specified)"}

        provider, default_timeframe = _build_history_provider(args.provider, settings)
        timeframe = args.timeframe or default_timeframe

        candles_by_symbol: dict[str, list[OHLCV]] = {}
        for symbol in symbols:
            candles = await provider.fetch_history(symbol, timeframe, start, end)
            setup.info("history fetched", symbol=symbol, timeframe=timeframe, candles=len(candles))
            if candles:
                candles_by_symbol[symbol] = candles

        backtester = DecisionReplayBacktester(
            risk_settings=settings.risk,
            initial_cash=settings.execution.initial_cash,
            fee_pct=settings.execution.paper_fee_pct,
            slippage_pct=settings.execution.paper_slippage_pct,
        )
        report = await backtester.replay(decisions, candles_by_symbol, timeframe=timeframe)

        _print_summary(report.to_dict())  # type: ignore[arg-type]
        if args.report:
            await asyncio.to_thread(_write_report, args.report, report.to_dict())
            setup.info("report written", path=args.report)
        return report.to_dict()
    finally:
        if provider is not None:
            await provider.close()
        await storage.close()


def _print_summary(report: dict[str, Any]) -> None:
    print("\n=== Decision-replay backtest (§7.14) ===")
    print(f"window          : {report['start']} → {report['end']} ({report['timeframe']} candles)")
    print(f"symbols         : {', '.join(report['symbols'])}")
    print(
        f"equity          : {report['initial_cash']:.2f} → {report['final_equity']:.2f} "
        f"({report['total_return_pct']:+.2f}%)"
    )
    blended = report["blended_buy_and_hold_pct"]
    if blended is not None:
        print(f"buy & hold      : {blended:+.2f}% (equal-weighted) {report['buy_and_hold_pct']}")
    print(f"max drawdown    : {report['max_drawdown_pct']:.2f}%")
    print(f"sharpe          : {report['sharpe']:.2f} (annualized, coarse — see module docstring)")
    wr = report["win_rate"]
    if wr is not None:
        print(f"closed trades   : {report['closed_trades']} (win rate {wr:.0%})")
    else:
        print("closed trades   : 0")
    if report.get("avg_win") is not None:
        print(f"avg win / loss  : {report['avg_win']:+.2f} / {(report['avg_loss'] or 0):+.2f}")
    print(
        f"auto exits      : {report['auto_exits']} | risk-rejected: {report['risk_rejected']} "
        f"| holds: {report['holds']}"
    )
    for symbol, stats in report["per_symbol"].items():
        print(
            f"  {symbol:<12} trades={stats['trades']} wins={stats['wins']} "
            f"losses={stats['losses']} realized_pnl={stats['realized_pnl']:+.2f}"
        )


def _write_report(path: str, report: dict[str, Any]) -> None:
    """Blocking JSON dump, pushed off the event loop by ``asyncio.to_thread``."""
    with open(path, "w") as f:
        json.dump(report, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay stored decisions against fresh historical candles (no LLM calls)."
    )
    parser.add_argument("--start", default=None, help="Window start (YYYY-MM-DD or ISO datetime)")
    parser.add_argument("--end", default=None, help="Window end (default: now)")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look-back window when --start is omitted (default 30)",
    )
    parser.add_argument(
        "--symbols", nargs="*", default=None, help="Symbols (default: those with stored decisions)"
    )
    parser.add_argument(
        "--provider",
        choices=["ccxt", "yfinance"],
        default="ccxt",
        help="Candle source (default ccxt = crypto_agent.exchange public data)",
    )
    parser.add_argument(
        "--timeframe",
        default=None,
        help="Candle timeframe (default: 1h for ccxt, 1d for yfinance)",
    )
    parser.add_argument("--report", default=None, help="Write the full JSON report to this path")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
