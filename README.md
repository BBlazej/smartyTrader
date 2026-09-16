# Autonomous Trading Agent

Paper-trading agents powered by a local LLM (LM Studio). Two agents — **crypto**
(Kraken testnet) and **stocks** (XTB demo) — share one decision pipeline, one
deterministic risk engine, and one storage layer.

> **Safety-first:** the paper executor is the default and nothing executes
> without passing the risk gate. The LLM proposes; a hard-coded risk engine
> disposes. Live trading is never assumed.

## How it works

```
fetch data → compute indicators → build prompt → call LLM
        → parse TradeSignal → risk check (RiskResult)
        → execute if approved → persist (decision / order / portfolio)
```

The LLM never bypasses the risk engine. If any rule is violated, the signal is
rejected with a reason and nothing is sent to the exchange.

### What makes it different

- **Learns from its own track record.** Each cycle feeds the LLM the agent's last
  N prior decisions — action, confidence, reasoning, risk verdict — *and the
  realized PnL once each position closed* (net of fees). The model sees what it
  decided **and how it turned out**, so it can avoid repeating losing patterns.
- **Realistic paper PnL.** The paper executor models per-side fees and slippage,
  so realized PnL (and the win/loss the risk engine tracks) is net-of-fee —
  comparable to the "win rate > 50% after fees" live-readiness gate.
- **Two markets, one core.** The crypto and stocks agents differ only in data
  source and execution adapter; the pipeline, risk engine, storage, and alerts
  are shared.

## Quickstart

The project runs from the repo root with `src/` as the top-level package (no
installation step needed).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # runtime deps + pytest/hypothesis/ruff (see pyproject.toml)
pip install -e ".[stocks]"   # adds yfinance — only needed for stocks data

cp .env.example .env        # add your keys (or run in paper mode)

pytest                      # 260 tests, no network needed
python -m scripts.run_crypto_agent   # run the crypto agent (paper by default)
python -m scripts.run_stocks_agent   # run the stocks agent (paper by default)
python -m scripts.run_crypto_agent --once   # exactly one cycle, then exit
```

A disabled agent (`crypto_agent.enabled: false` / `stocks_agent.enabled: false`)
exits immediately **without running anything** — no cycles, LLM calls, order
placement or DB writes. Single-cycle mode is the explicit `--once` flag, never
a side effect of disabling an agent.

**Paper mode is the default.** The crypto agent always runs on **live public
market data** — Kraken's public OHLCV endpoint needs no API key and no sandbox
mode, so even the paper path generates real snapshots, indicators, and LLM
signals. The **first cycle runs immediately at startup**, then repeats every
`interval_minutes`. Execution stays simulated: without `KRAKEN_API_KEY`/`KRAKEN_API_SECRET`
in `.env` (the runner loads it for you — no `python-dotenv` needed), orders are
filled by the fee/slippage-aware `PaperExecutor`; with those set, orders go to
the Kraken testnet via a separate sandboxed, keyed client. The stocks runner
always uses the `PaperExecutor` until the XTB demo OAuth2 flow lands.

## Project layout

```
src/
├── core/
│   ├── models.py             # Pydantic models (TradeSignal, DecisionRecord, Position, OrderResult, Executor protocol)
│   ├── config.py             # YAML + env settings loader
│   ├── llm_client.py         # LM Studio HTTP client (retry + JSON parse + HOLD fallback)
│   ├── risk_engine.py        # 7 deterministic risk rules (all live)
│   ├── storage.py            # SQLite (SQLAlchemy + aiosqlite) repository
│   ├── decision_pipeline.py  # fetch → indicators → prompt → LLM → risk → execute (+ decision-history loop)
│   └── scheduler.py          # APScheduler wrapper
├── data/
│   ├── ccxt_provider.py      # Crypto OHLCV via CCXT (Kraken)
│   └── xtb_provider.py       # Stocks OHLCV (yfinance source; xAPI is the seam)
├── execution/
│   ├── paper_executor.py     # Simulated executor (default; fee + slippage + net PnL)
│   ├── kraken_executor.py    # Kraken testnet orders via CCXT
│   └── xtb_executor.py       # XTB demo orders (xAPI seam)
├── agents/
│   ├── crypto_agent.py       # Crypto cycle: pipeline + risk tracking + persistence
│   └── stocks_agent.py       # Stocks cycle + market-hours guard
├── analysis/
│   └── __init__.py           # (empty — indicators & prompt currently live in core)
└── monitoring/
    ├── logger.py             # structlog setup
    └── alerts.py             # AlertManager + sinks (Noop)

scripts/
├── run_crypto_agent.py       # Entry point — wires config → core → scheduler (crypto)
└── run_stocks_agent.py       # Entry point — wires config → core → scheduler (stocks)

config/settings.yaml          # All tunables (LLM, pairs, risk, execution, monitoring)
tests/
├── unit/                     # Fast, no network
└── integration/              # Full pipeline, mocked provider, real SQLite
```

## Configuration

Everything is driven by `config/settings.yaml` + `.env` — no hard-coded
thresholds. Key sections:

| Section | What it controls |
|---|---|
| `llm` | LM Studio endpoint, model, timeout, retries, JSON-schema opt-in |
| `crypto_agent` | enabled, exchange, testnet flag, interval, pairs, `decision_history_limit` |
| `stocks_agent` | enabled, broker, demo, interval, `market_hours`, `market_timezone` (zone the window is in), symbols, `decision_history_limit` |
| `risk` | max position %, daily loss limit, max drawdown, cooldown, max positions, min confidence |
| `execution` | paper-executor fee % and slippage % (so paper PnL is realistic) |
| `storage` | SQLite path (WAL mode — concurrent reads while the agent writes) |
| `monitoring` | log level, alert dedup window |

### Environment variables

| Variable | Effect |
|---|---|
| `LM_STUDIO_ENDPOINT` | Override the LLM endpoint |
| `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` | Enable Kraken testnet execution (else paper); public data works in both modes with no key |
| `LM_STUDIO_USE_JSON_SCHEMA` | Opt-in strict JSON response mode |
| `XTB_API_KEY` | Reserved for XTB demo (still paper until the OAuth2 flow lands) |

## Running & testing

```bash
pytest                          # all tests (unit + integration) with coverage
pytest tests/unit/ -k risk      # a subset
ruff check .                    # lint
ruff format .                   # format (100-char lines)
```

Tests mock all external dependencies — **no real network calls** in the suite.

## Safety rules

- **Paper executor is the default.** Never assume live trading.
- **Risk engine runs before every order.** Nothing executes without approval.
- **API keys live in `.env`** — never committed, never logged (see `.gitignore`).
- **Risk rules are hard-coded, deterministic guards** — not LLM decisions.
- **`enabled: false` means nothing runs.** The runner exits before constructing
  any component; use `--once` when a single intentional cycle is wanted.

## Status

Phases 1–5 complete: shared core (LLM client, risk engine, storage, scheduler,
decision pipeline with inline indicators + prompt), crypto provider + executor +
agent, **stocks provider + executor + agent**, paper executor with
fee/slippage modeling, monitoring (structured logging), both
entry scripts, the **learn-from-your-own-track-record loop** (prior decisions
+ realized PnL fed back to the LLM), and the **crypto agent on real data**
(the paper path fetches live public Kraken OHLCV — no API key needed — while
execution stays simulated), the **timezone-aware market-hours guard** (the
stocks window is compared in the config-driven `market_timezone`, so a UTC host
stays correct), **SQLite WAL mode** (concurrent reads while the agent writes),
and **per-cycle position marking** (open paper positions are re-marked at each
snapshot's last close before the risk check, so unrealized PnL and the
daily-loss rule track the market), and **honest `enabled: false` semantics**
(both runners exit without running anything when an agent is disabled; `--once`
is the explicit single-cycle flag), and the **drawdown guard is live** (peak
equity high-water mark persisted via SQLite, seeded at startup) with the
**order-size cap enforced at the gate** (oversized plans are rejected before
execution; sells clamp to units held). **260 tests passing at ~95% coverage.**

Not yet built: news/sentiment + economic-calendar feeds, `scripts/backtest.py`,
the XTB demo OAuth2 flow, and a dashboard. See `PLAN.md` §7 (Gaps & Next Steps)
for the full list — reordered after the 2026-09-15 full-codebase review
(low-hanging fruit first, then High → Low severity); its detailed findings
live in `review.MD` at the repo root.
