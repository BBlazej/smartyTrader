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

pytest                      # 436 tests, no network needed
python -m scripts.run_crypto_agent   # run the crypto agent (paper by default)
python -m scripts.run_stocks_agent   # run the stocks agent (paper by default)
python -m scripts.run_crypto_agent --once   # exactly one cycle, then exit
python -m scripts.backtest --days 30        # replay stored decisions vs fresh candles (§7.14)
python -m scripts.run_dashboard             # web dashboard at http://127.0.0.1:8080 (§7.15 P3/P4)

# or run the whole system in containers (§7.15 P5):
docker compose up -d --build                # agents + dashboard (loopback 127.0.0.1:8080)
docker compose run --rm backtester --days 30  # on-demand replay (tools profile)
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
│   ├── decision_pipeline.py  # fetch → indicators → prompt → LLM → risk → persist decision → execute
│   ├── rehydration.py        # Restores paper book + risk trackers from SQLite at startup
│   ├── retention.py          # Fail-soft storage pruning wrapper (startup + scheduled)
│   ├── runner.py             # Shared runner lifecycle: enabled-gate, wiring, --once/scheduled loops
│   ├── backtester.py         # Decision-replay backtester: same risk/fee model, zero LLM calls (§7.14)
│   ├── control_api.py        # Agent-side FastAPI control API (pause/resume/close-all/config) (§7.15)
│   ├── control_config.py     # Safe config-override whitelist (credentials structurally impossible) (§7.15)
│   └── scheduler.py          # APScheduler wrapper
├── data/
│   ├── ccxt_provider.py      # Crypto OHLCV via CCXT (Kraken)
│   └── xtb_provider.py       # Stocks OHLCV (yfinance source; xAPI is the seam)
├── execution/
│   ├── paper_executor.py     # Simulated executor (default; fee + slippage + net PnL)
│   ├── position_tracker.py   # Shared FIFO cost-basis ledger → realized PnL per entry decision
│   ├── kraken_executor.py    # Kraken testnet orders via CCXT
│   └── xtb_executor.py       # XTB demo orders (xAPI seam)
├── agents/
│   ├── base_agent.py         # Shared cycle loop, post-process, persistence, alerts (§7.13)
│   ├── crypto_agent.py       # Thin subclass (24/7, no hours guard)
│   └── stocks_agent.py       # Thin subclass + market-hours guard (weekend/holiday/wrap)
├── analysis/
│   └── __init__.py           # (empty — indicators & prompt currently live in core)
├── monitoring/
│   ├── logger.py             # structlog setup
│   └── alerts.py             # AlertManager + sinks (Noop)
└── dashboard/                # Web UI (§7.15 P3/P4): FastAPI + Jinja2/HTMX, reads the WAL DB
    ├── app.py                # Pages + HTMX control endpoints (latch writes; SafeConfigOverrides form)
    ├── views.py              # Pure view-models: win-rate/confidence stats, uPlot shaping, positions
    └── templates/            # base / overview / decisions / positions / config / _health

scripts/
├── run_crypto_agent.py       # Entry point — crypto-specific factories + shared runner
├── run_stocks_agent.py       # Entry point — stocks-specific factories + shared runner
├── run_dashboard.py          # Web dashboard server (monitor + control + safe config) (§7.15)
├── prune_storage.py          # Out-of-band retention pruning (no agents, no trades)
└── backtest.py               # Decision replay vs fresh historical candles (CLI + JSON report)

config/settings.yaml          # All tunables (LLM, pairs, risk, execution, monitoring)
Dockerfile                    # Slim image (python:3.11, non-root) for all services (§7.15 P5)
docker-compose.yml            # agent-crypto/-stocks + dashboard + on-demand backtester (§7.15 P5)
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
| `stocks_agent` | enabled, broker, demo, interval, `market_hours` (wrap-around windows supported), `market_timezone` (zone the window is in), `market_holidays` (ISO closure dates; weekends always closed), symbols, `decision_history_limit` |
| `risk` | max position %, daily loss limit, max drawdown, cooldown, max positions, min confidence, `enforce_exit_levels` (deterministic SL/TP closes) |
| `execution` | paper-executor fee %, slippage %, and `initial_cash` (seeds a fresh portfolio; persisted state wins after the first cycle) |
| `storage` | SQLite path (WAL mode — concurrent reads while the agent writes), retention windows: `snapshot_retention_days` (default 30), `history_retention_days` (0 = keep forever), `prune_interval_minutes` |
| `monitoring` | log level, alert dedup window |
| `control_api` | agent-side control API: `enabled` (default false), `host` (loopback), per-agent ports (§7.15) |
| `dashboard` | web dashboard bind (`host`/`port`, loopback defaults), HTMX `refresh_seconds`, `agents` shown/controlled (§7.15 P3/P4) |

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
execution; sells clamp to units held), and the **keyed Kraken path hardened
against real ccxt payloads** (nested balances, fill price/time recording,
graceful spot `fetch_positions` degradation — live testnet smoke still pending),
and **restart-safe paper state** (cash/positions rehydrate from the latest
portfolio snapshot; daily-loss baseline and losing-streak/cooldown rebuild from
persisted outcomes; `execution.initial_cash` is config-driven), and **honest
outcome attribution** (one shared FIFO tracker gives every executor's closing
fills a `realized_pnl` plus per-entry-decision `closed_entries`, so the PnL of a
closed position lands back on the buy decision that opened it; LLM-unavailable
fallback HOLDs are stored for audit but never re-fed into prompts, and each live
decision's full prompt+response is logged), and **deterministic stop-loss /
take-profit exits** (levels ride on the position through restarts; a breach is
closed on the next cycle without asking the LLM or the risk gate — toggle with
`risk.enforce_exit_levels`), and **decision-replay backtesting** (re-simulates the
agent's own stored decisions against fresh historical candles through the same risk
engine + fee/slippage model — deterministic, zero LLM calls; `scripts/backtest.py`),
and a **web dashboard** (FastAPI + Jinja2/HTMX: portfolio chart, positions, decisions
with win-rate/confidence stats, agent health; HTMX pause/resume/close-all controls and a
safe-config editor — all writing the same `agent_control` latches; `scripts/run_dashboard.py`,
§7.15 P3/P4), now packaged for containers (`docker compose up -d --build` — agents, dashboard
and an on-demand backtester on one shared SQLite volume; §7.15 P5).
**436 tests passing at ~93% coverage.**

Not yet built: news/sentiment + economic-calendar feeds
and the XTB demo OAuth2 flow. See `PLAN.md` §7 (Gaps & Next Steps)
for the full list — reordered after the 2026-09-15 full-codebase review
(low-hanging fruit first, then High → Low severity); its detailed findings
live in `review.MD` at the repo root, with follow-up undocumented TODO items tracked in `review2.md`.

## Documentation map

| File | Contents |
|---|---|
| `README.md` (this file) | overview & quickstart |
| `ARCHITECTURE.md` | architecture: components, data flow, storage schema, control plane, design decisions (Mermaid diagrams) |
| `HISTORY.md` | delivered work: status snapshot, original Phase 1–2 plans, completed §7 items |
| `PLAN.md` | gaps, todos & next steps (§7), Phase 4 iteration, risk register |
| `AGENTS.md` | agent-facing facts & rules for coding agents |
| `nightly_finds.md` | bugs/gaps discovered during development |
