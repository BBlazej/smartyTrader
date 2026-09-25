"""Entry point: run the crypto agent — paper by default.

Shared lifecycle (enabled gate, storage/LLM/risk wiring, drawdown seeding, rehydration,
retention pruning, ``--once`` vs scheduled loop, guaranteed cleanup) lives in
:func:`src.core.runner.run_agent` (§7.13). This script keeps only the crypto-specific
wiring: live public CCXT data from ``crypto_agent.exchange`` (OKX Europe, ccxt
``myokx``, §7.64) + paper execution by default. A keyed executor needs
``EXCHANGE_API_KEY`` (+ ``_SECRET``, and ``_PASSPHRASE`` on OKX) **and** either the
exchange's sandbox/demo (``testnet: true``) or — for live money —
``crypto_agent.live_trading: true`` plus ``LIVE_TRADING_ACK`` (§7.41); anything short of
that stays on paper and says why.

``crypto_agent.enabled: false`` means **do nothing**: the runner exits before
constructing any component — no cycles, LLM calls, order placement or DB writes.
For an intentional single-cycle run (e.g. cron), pass ``--once``, which runs exactly
one full cycle and exits cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import structlog

from src.agents.crypto_agent import CryptoAgent
from src.core.config import (
    LIVE_TRADING_ACK_ENV,
    LIVE_TRADING_ACK_PHRASE,
    Settings,
    live_trading_acknowledged,
)
from src.core.runner import RunnerAlreadyRunning, build_alerts, load_dotenv, run_agent
from src.data.ccxt_provider import create_ccxt_provider, exchange_has_sandbox
from src.execution.ccxt_executor import create_ccxt_executor
from src.execution.paper_executor import PaperExecutor
from src.monitoring import setup_logging

#: Keyed-venue credentials (§7.64): generic names — the exchange is config-driven.
API_KEY_ENV = "EXCHANGE_API_KEY"
API_SECRET_ENV = "EXCHANGE_API_SECRET"
API_PASSPHRASE_ENV = "EXCHANGE_API_PASSPHRASE"


def _build_data_and_execution(settings: Settings) -> tuple[object, object, str]:
    """Build the (provider, executor) pair plus a mode label.

    The **data feed is always live public market data** from the configured exchange
    (public OHLCV endpoints need no API key and no sandbox mode). Only *execution*
    changes:

    - no ``EXCHANGE_API_KEY`` → :class:`PaperExecutor` (safe default), mode ``paper``;
    - key + ``testnet: true`` on an exchange with a ccxt sandbox/demo → keyed executor
      there, mode ``<exchange>-sandbox`` (OKX demo trading);
    - key + ``testnet: false`` + ``live_trading: true`` + ``LIVE_TRADING_ACK`` →
      keyed executor on the **live** venue, mode ``<exchange>-LIVE``;
    - any other keyed combination → paper, with a warning saying exactly why (§7.41 —
      ``testnet: false`` alone must never silently trade real funds).
    """
    cfg = settings.crypto_agent
    exchange = cfg.exchange
    if not exchange:
        raise SystemExit("crypto_agent.exchange is not set (e.g. 'myokx' for OKX Europe)")
    log = structlog.get_logger().bind(component="runner")

    provider = create_ccxt_provider(exchange_id=exchange, testnet=False)

    if os.getenv(API_KEY_ENV):
        mode = _keyed_mode(settings, exchange, log)
        if mode is not None:
            # A dedicated, keyed client for order placement (public data above
            # intentionally stays on the main endpoint).
            order_client = create_ccxt_provider(
                exchange_id=exchange,
                testnet=mode.endswith("-sandbox"),
                api_key=os.getenv(API_KEY_ENV),
                api_secret=os.getenv(API_SECRET_ENV),
                api_passphrase=os.getenv(API_PASSPHRASE_ENV),
            ).client
            # e.g. "myokx-sandbox" / "myokx-live" — tags its rows (§7.61).
            executor = create_ccxt_executor(
                order_client, quote_currency=cfg.quote_currency, venue=mode.lower()
            )
            return provider, executor, mode

    executor = PaperExecutor(
        initial_cash=settings.execution.initial_cash,
        slippage_pct=settings.execution.paper_slippage_pct,
        fee_pct=settings.execution.paper_fee_pct,
    )
    return provider, executor, "paper"


def _keyed_mode(settings: Settings, exchange: str, log: object) -> str | None:
    """Mode for a keyed executor, or ``None`` (stay on paper) — never live by accident."""
    cfg = settings.crypto_agent
    if getattr(cfg, "testnet", True):
        if exchange_has_sandbox(exchange):
            return f"{exchange}-sandbox"
        log.warning(  # type: ignore[attr-defined]
            f"{API_KEY_ENV} is set but '{exchange}' has no sandbox in ccxt — staying on the "
            "paper executor. (To trade it live on purpose set crypto_agent.testnet: false, "
            f"crypto_agent.live_trading: true and {LIVE_TRADING_ACK_ENV}={LIVE_TRADING_ACK_PHRASE}.)"
        )
        return None
    if not getattr(cfg, "live_trading", False) or not live_trading_acknowledged():
        log.warning(  # type: ignore[attr-defined]
            f"{API_KEY_ENV} is set with testnet: false — that is REAL money, but live trading "
            "is not acknowledged; staying on the paper executor. Requires BOTH "
            f"crypto_agent.live_trading: true and {LIVE_TRADING_ACK_ENV}={LIVE_TRADING_ACK_PHRASE}."
        )
        return None
    return f"{exchange}-LIVE"


def _make_components(settings: Settings) -> tuple[object, object]:
    """Build provider + executor and log which mode was chosen."""
    provider, executor, mode = _build_data_and_execution(settings)
    log = structlog.get_logger().bind(component="runner")
    if mode == "paper":
        log.info(
            f"using live public data + paper executor (no {API_KEY_ENV} set)",
            fee_pct=settings.execution.paper_fee_pct,
            slippage_pct=settings.execution.paper_slippage_pct,
        )
    elif mode.endswith("-LIVE"):
        log.warning(
            "LIVE TRADING: keyed executor on the real venue — orders use real funds",
            mode=mode,
        )
    else:
        log.info("using live public data + keyed sandbox executor", mode=mode)
    return provider, executor


async def run(run_once: bool = False) -> None:
    load_dotenv()
    settings = Settings()
    setup_logging(settings.monitoring.log_level)

    await run_agent(
        settings,
        component="crypto",
        agent_enabled=settings.crypto_agent.enabled,
        interval_minutes=settings.crypto_agent.interval_minutes,
        decision_history_limit=settings.crypto_agent.decision_history_limit,
        job_id="crypto_cycle",
        timeframe=settings.crypto_agent.timeframe or "1h",
        decide_on_new_bar_only=settings.crypto_agent.decide_on_new_bar_only,
        build_components=lambda: _make_components(settings),
        build_agent=lambda pipeline, storage, risk_engine, llm_client: CryptoAgent(
            pipeline=pipeline,
            storage=storage,
            risk_engine=risk_engine,
            llm_client=llm_client,
            pairs=settings.crypto_agent.pairs,
            timeframe=settings.crypto_agent.timeframe or "1h",
            alerts=build_alerts(settings),
        ),
        run_once=run_once,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the crypto agent on a schedule. A disabled agent "
            "(crypto_agent.enabled: false) exits without running anything."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one decision cycle and exit instead of the scheduled loop.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(run_once=args.once))
    except KeyboardInterrupt:
        pass
    except RunnerAlreadyRunning:
        # §7.52: another runner owns this agent; run_agent logged the reason.
        # Distinct exit code so cron/systemd notices a refused double-start.
        raise SystemExit(2)


if __name__ == "__main__":
    main()
