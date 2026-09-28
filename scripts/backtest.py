"""Backtest entry point — decision replay over stored decisions (§7.14).

Replays the agent's own stored ``llm_decisions`` against fresh historical candles,
through the same risk engine + fee/slippage model as live. Deterministic; **zero LLM
calls** (the LLM replay variant is a separate, later experiment).

Examples:
    python -m scripts.backtest --start 2026-08-01 --end 2026-09-15
    python -m scripts.backtest --days 30 --symbols BTC/USDT ETH/USDT --timeframe 1h
    python -m scripts.backtest --provider yfinance --symbols AAPL --days 90 --report out.json
    python -m scripts.backtest --strategy crypto_position --days 60   # one sleeve (§7.73)
    python -m scripts.backtest --mode demo                            # the demo book (§7.78)

The stored decisions come from **one book** (``<data_dir>/<mode>_<agent>.db``, §7.78):
``--mode paper|demo|real`` (default ``paper``) picks it, and the agent follows the
candle source (ccxt → crypto, yfinance → stocks) unless ``--agent`` says otherwise.

``--strategy NAME`` replays one strategy sleeve (§7.71) on its own terms: only its
decisions, its timeframe, its effective risk limits and ``weight × initial_cash``.
Every report compares the replay with dumb baselines on the same symbols, period and
cost model — buy & hold, a 20/50 MA crossover, cash (§7.73).

The candle source is fresh from the venue (the configured exchange's public data via CCXT, or yfinance):
the agent does not run 24/7, so stored ``market_snapshots`` alone are too sparse.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from src.core.backtester import DecisionReplayBacktester, ReplayDecision
from src.core.config import Settings, SleeveSpec
from src.core.db_layout import MODES, db_path
from src.core.models import OHLCV
from src.core.sleeves import sleeve_risk_settings
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


def _sleeve_spec(settings: Settings, agent: str, name: str | None) -> SleeveSpec | None:
    """The configured sleeve ``name`` of ``agent`` (``SystemExit`` when unknown)."""
    if name is None:
        return None
    spec = getattr(getattr(settings, f"{agent}_agent", None), "sleeves", None)
    found = spec.get(name) if spec is not None else None
    if found is None:
        raise SystemExit(f"'{name}' is not a configured sleeve of the {agent} agent")
    return found


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    setup = structlog.get_logger().bind(component="backtest")
    agent = getattr(args, "agent", None) or ("stocks" if args.provider == "yfinance" else "crypto")
    mode = getattr(args, "mode", "paper")
    sleeve = _sleeve_spec(settings, agent, getattr(args, "strategy", None))

    end = _parse_dt(args.end, end_of_day=True) if args.end else datetime.now(UTC)
    start = _parse_dt(args.start) if args.start else end - timedelta(days=args.days)
    if start >= end:
        raise SystemExit("--start must be before --end")

    # One book's decisions (§7.78) — replay never mixes modes.
    book = db_path(settings.storage.data_dir, mode, agent)
    if not Path(book).exists():
        raise SystemExit(f"no {mode} book for the {agent} agent at {book} — run that agent first")
    setup.info("replaying book", mode=mode, agent=agent, database=str(book))
    storage = Storage(str(book))
    await storage.initialize()
    provider = None
    try:
        # Decisions belong to one agent (§7.39): ccxt candles replay the crypto
        # agent's decisions, yfinance the stocks agent's.
        rows = await storage.get_decisions_in_range(
            start=start,
            end=end,
            symbols=(args.symbols or None),
            agent=agent,
            **({"strategy": sleeve.name} if sleeve is not None else {}),
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
        timeframe = args.timeframe or (sleeve.timeframe if sleeve else None) or default_timeframe

        candles_by_symbol: dict[str, list[OHLCV]] = {}
        for symbol in symbols:
            candles = await provider.fetch_history(symbol, timeframe, start, end)
            setup.info("history fetched", symbol=symbol, timeframe=timeframe, candles=len(candles))
            if candles:
                candles_by_symbol[symbol] = candles

        # Replay with the same per-venue cost profile the live agent would use
        # (§7.65): ccxt candles replay the crypto agent's schedule, yfinance the
        # stocks agent's.
        cost_agent = "stocks" if args.provider == "yfinance" else "crypto"
        costs = settings.execution.paper_cost_params(cost_agent)
        risk_settings = settings.risk
        initial_cash = settings.execution.initial_cash
        if sleeve is not None:
            # The sleeve's own limits and capital share, as live (§7.71).
            risk_settings = sleeve_risk_settings(
                settings.risk_baseline, settings.risk, sleeve.risk_overrides
            )
            initial_cash *= sleeve.weight or 1.0
        backtester = DecisionReplayBacktester(
            risk_settings=risk_settings,
            initial_cash=initial_cash,
            fee_pct=costs["paper_fee_pct"],
            slippage_pct=costs["paper_slippage_pct"],
            min_commission=costs["paper_min_commission"],
            fx_fee_pct=costs["paper_fx_fee_pct"],
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
    baselines = report.get("baselines_pct") or {}
    if baselines:
        shown = ", ".join(
            f"{name} {value:+.2f}%" for name, value in baselines.items() if value is not None
        )
        verdict = {True: "BEATS", False: "does NOT beat", None: "n/a"}[
            report.get("beats_best_baseline")
        ]
        print(f"baselines (net) : {shown}")
        print(f"vs best baseline: {verdict} ({report.get('best_baseline')})")
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
    parser.add_argument(
        "--strategy",
        default=None,
        help="Replay one strategy sleeve: its decisions, timeframe, risk limits, capital (§7.73)",
    )
    parser.add_argument(
        "--agent",
        choices=["crypto", "stocks"],
        default=None,
        help="Whose decisions to replay (default: from --provider, §7.78)",
    )
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default="paper",
        help="Book to replay decisions from: paper | demo | real (default paper, §7.78)",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
