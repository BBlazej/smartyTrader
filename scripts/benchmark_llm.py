"""Benchmark LLM decision latency on the REAL prompt (§7.69).

The watchlist size — and whether two sleeves fit an hourly clock (CHANGE.md Q5) —
depends on how long one decision takes on the user's GPU. Live percentiles are now
recorded per decision (stored on ``llm_decisions``, shown as p50/p95 on the
dashboard); this CLI produces the numbers *before* committing to a watchlist: it
builds genuine prompts (live candles + indicators + YOUR BOOK placeholder, exactly
what :class:`DecisionPipeline` sends) and times real ``ask_trade_signal`` calls.

Examples:
    python -m scripts.benchmark_llm                       # configured pairs, 3 calls each
    python -m scripts.benchmark_llm --symbols BTC/EUR --repeats 5 --warmup 2
    python -m scripts.benchmark_llm --report data/llm_bench.json

Requires LM Studio running at ``llm.endpoint`` and network for fresh candles.
The first ``--warmup`` calls per symbol are excluded from the percentiles (a cold
model or cache skews them). The closing guidance sizes a watchlist for the given
bar interval at ``--budget`` utilization of it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from typing import Any

import structlog

from src.analysis.indicators import compute_indicators
from src.analysis.prompt_builder import DEFAULT_SYSTEM_PROMPT, build_user_prompt
from src.core.config import Settings
from src.core.llm_client import LLMClient


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    log = structlog.get_logger().bind(component="llm_benchmark")

    provider_kind = args.provider
    if provider_kind == "yfinance":
        from src.data.xtb_provider import create_xtb_provider

        provider = create_xtb_provider()
        timeframe = args.timeframe or settings.stocks_agent.timeframe or "1d"
        symbols = args.symbols or list(settings.stocks_agent.symbols)
    else:
        from src.data.ccxt_provider import create_ccxt_provider

        exchange = settings.crypto_agent.exchange
        if not exchange:
            raise SystemExit("crypto_agent.exchange is not set (e.g. 'myokx')")
        provider = create_ccxt_provider(exchange_id=exchange, testnet=False)
        timeframe = args.timeframe or settings.crypto_agent.timeframe or "1h"
        symbols = args.symbols or list(settings.crypto_agent.pairs)

    llm = LLMClient(settings.llm)
    samples: list[dict[str, Any]] = []
    failures = 0
    try:
        # Build the genuine prompt per symbol first — data fetch is not timed.
        prompts: dict[str, str] = {}
        for symbol in symbols:
            snapshot = await provider.fetch_snapshot(symbol, timeframe)
            snapshot.indicators = compute_indicators(snapshot.candles)
            prompts[symbol] = build_user_prompt(snapshot)
            log.info("prompt built", symbol=symbol, chars=len(prompts[symbol]))

        for round_no in range(args.repeats):
            for symbol in symbols:
                warmup = round_no < args.warmup
                signal = await llm.ask_trade_signal(
                    system_prompt=DEFAULT_SYSTEM_PROMPT, user_prompt=prompts[symbol]
                )
                metrics = llm.last_metrics
                latency_ms = (
                    metrics.latency_ms
                    if metrics is not None
                    else float("nan")  # should not happen with LLMClient
                )
                fallback = bool(getattr(signal, "is_fallback", False))
                failures += int(fallback)
                samples.append(
                    {
                        "symbol": symbol,
                        "round": round_no,
                        "warmup": warmup,
                        "fallback": fallback,
                        "latency_ms": latency_ms,
                        "prompt_tokens": metrics.prompt_tokens if metrics else None,
                        "completion_tokens": metrics.completion_tokens if metrics else None,
                    }
                )
                log.info(
                    "call done",
                    symbol=symbol,
                    round=round_no,
                    warmup=warmup,
                    latency_ms=round(latency_ms, 1),
                    completion_tokens=metrics.completion_tokens if metrics else None,
                    fallback=fallback,
                )

        timed = [s for s in samples if not s["warmup"] and s["latency_ms"] == s["latency_ms"]]
        latencies_ms = [float(s["latency_ms"]) for s in timed]
        completion_tokens = [
            float(s["completion_tokens"]) for s in timed if s["completion_tokens"] is not None
        ]
        report: dict[str, Any] = {
            "model": settings.llm.model,
            "timeframe": timeframe,
            "symbols": symbols,
            "repeats": args.repeats,
            "warmup": args.warmup,
            "samples": samples,
            "fallbacks": failures,
            "latency_ms": {
                "count": len(latencies_ms),
                "p50": _percentile(latencies_ms, 0.50),
                "p95": _percentile(latencies_ms, 0.95),
                "max": max(latencies_ms) if latencies_ms else None,
            },
            "tokens": {
                "avg_completion": statistics.fmean(completion_tokens)
                if completion_tokens
                else None,
            },
        }
        _print_summary(report, budget=args.budget)
        if args.report:
            # Blocking JSON dump pushed off the event loop (same as backtest.py).
            await asyncio.to_thread(_write_report, args.report, report)
            log.info("report written", path=args.report)
        return report
    finally:
        await provider.close()
        await llm.close()


def _write_report(path: str, report: dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(report, f, indent=2)


def _print_summary(report: dict[str, Any], *, budget: float) -> None:
    lat = report["latency_ms"]
    print("\n=== LLM decision benchmark (§7.69) ===")
    print(f"model     : {report['model']}  ({report['timeframe']} prompts)")
    print(f"symbols   : {', '.join(report['symbols'])}")
    print(f"calls     : {len(report['samples'])} ({report['warmup']} warmup rounds excluded)")
    if report["fallbacks"]:
        print(f"⚠ fallbacks: {report['fallbacks']} (LLM unavailable — numbers include retry cost)")
    if not lat["count"]:
        print("no timed samples — is LM Studio running at llm.endpoint?")
        return
    print(
        f"latency   : p50 {lat['p50'] / 1000:.1f}s | p95 {lat['p95'] / 1000:.1f}s "
        f"| max {lat['max'] / 1000:.1f}s"
    )
    tok = report["tokens"]["avg_completion"]
    if tok is not None:
        print(f"tokens    : ~{tok:.0f} completion tokens/decision")
    # Watchlist sizing guidance (CHANGE.md Q5): decisions must fit the bar clock.
    p95_s = lat["p95"] / 1000.0
    if p95_s > 0:
        for label, interval in (("hourly", 3600), ("daily", 86_400)):
            capacity = int(interval * budget / p95_s)
            print(
                f"watchlist : ~{capacity} symbols fit the {label} clock at p95 ({budget:.0%} used)"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Time real ask_trade_signal calls on genuine prompts (CHANGE.md Q5)."
    )
    parser.add_argument(
        "--symbols", nargs="*", default=None, help="Default: the agent's configured pairs"
    )
    parser.add_argument("--timeframe", default=None, help="Default: the agent's timeframe")
    parser.add_argument("--repeats", type=int, default=3, help="Calls per symbol (default 3)")
    parser.add_argument(
        "--warmup", type=int, default=1, help="Rounds excluded from percentiles (default 1)"
    )
    parser.add_argument(
        "--provider", choices=["ccxt", "yfinance"], default="ccxt", help="Candle source"
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=0.5,
        help="Max share of the bar interval spent deciding, for watchlist sizing (default 0.5)",
    )
    parser.add_argument("--report", default=None, help="Write the full JSON report to this path")
    args = parser.parse_args()
    # Guard against nonsense that would silently produce an empty benchmark.
    if args.repeats <= 0 or not (0 <= args.warmup < args.repeats):
        raise SystemExit("--repeats must be > 0 and --warmup in [0, repeats)")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
