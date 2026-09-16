# Autonomous Trading Agent — Project Plan

## Introduction

This document is the single source of truth for the autonomous trading agent project regarding what was done and what remains to be done.

## Overview

Two independent paper-trading agents sharing a common core:

| | Crypto Agent | Stocks Agent |
|---|---|---|
| **Exchange** | Kraken (testnet) | XTB (demo account) |
| **Data** | CCXT (OHLCV); news/sentiment — *planned* | xAPI + yfinance (OHLCV); economic calendar — *planned* |
| **LLM** | LM Studio → Qwen 3.8 27B (`qwen/qwen3.8-27b`) | Same shared LLM client |

Both agents use the same decision pipeline, risk engine, and storage layer — only the data sources and execution adapters differ.

> ### Status (as of this revision)
> **Built & tested (233 tests passing, ~95% coverage):** core (LLM client, risk engine, storage, scheduler, decision pipeline with inline indicators + prompt), crypto provider + executor + agent, **stocks provider (xAPI + yfinance) + executor + agent**, paper executor, monitoring (structured logging), both entry scripts, the **"learn from its own track record" loop** (the LLM now sees each prior decision **and its realized PnL outcome** — `realized_pnl` column + backfill on a closing order + rendered under `CONTEXT:`), **fee modeling in the paper executor** (`fee_pct` deducted from cash and from realized PnL, so paper PnL is net-of-fee), and the **crypto agent running on real data** (the paper path now fetches live public Kraken OHLCV via CCXT — no API key needed for public data — while execution stays simulated), the **timezone-aware market-hours guard** (localizes `now` to the config-driven `stocks_agent.market_timezone`, so a UTC host stays correct), **SQLite WAL mode** (concurrent readers — dashboard/backtester — while the agent writes), and **per-cycle position marking** (`DecisionPipeline` re-marks open paper positions at each snapshot's last close via the optional executor `update_price` hook before the risk check, so unrealized PnL, portfolio snapshots and the daily-loss rule track the market — §7.1). All config-driven via `decision_history_limit`, the `execution:` block, and `stocks_agent.market_timezone`.
> **Not yet implemented (do not assume these exist):** news/sentiment feed, economic-calendar feed, `analysis/indicators.py` + `analysis/prompt_builder.py` (indicators & prompt currently live inline in `core/decision_pipeline.py`), `scripts/backtest.py`, XTB demo OAuth2 flow, dashboard, the risk engine's **max-drawdown rule** (currently a stub — see §7.5), **real-venue realized-PnL backfill** (paper-only today — see §7.8), **portfolio/risk-state persistence across restarts** (memory-only today — see §7.7), and **stop-loss/take-profit enforcement on open positions** (levels recorded but never monitored — see §7.9). Full list incl. findings from the 2026-09-15 code review (`review.MD`): see §7 Gaps & Next Steps.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Shared Core                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ LLM      │  │ Risk     │  │ Storage  │  │ Scheduler  │  │
│  │ Client   │  │ Engine   │  │ (SQLite) │  │            │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬─────┘  │
│       │              │             │               │         │
├───────┼──────────────┼─────────────┼───────────────┼────────┤
│       ▼              ▼             ▼               ▼         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Decision Pipeline                        │   │
│  │  Market data → Feature extraction → LLM prompt       │   │
│  │  → Structured JSON signal → Risk gate → Order        │   │
│  └──────────────────────────────────────────────────────┘   │
├─────────────────────────┬───────────────────────────────────┤
│                         ▼                                   │
│            ┌──────────────────┐    ┌──────────────────┐     │
│            │   Crypto Agent   │    │   Stocks Agent   │     │
│            │                  │    │                  │     │
│            │ Data: CCXT       │    │ Data: xAPI       │     │
│            │ (sentiment: plan)│    │ + yfinance       │     │
│            │                  │    │ (econ cal: plan) │     │
│            │                  │    │                  │     │
│            │ Execute: Kraken  │    │ Execute: XTB     │     │
│            │   testnet        │    │   demo           │     │
│            └──────────────────┘    └──────────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Pipeline, Storage & Dashboard (Week 6)

This section captures the design decisions locked for Week 6 — the **data pipeline / DB / control architecture** that backtesting (§3.3), the dashboard, and the live agent all share. It is the source of truth for *how data flows*; §7.14 (backtest) and §7.15 (dashboard) are the deliverables built on it.

### Design decisions (locked)

| # | Decision | Chosen |
|---|---|---|
| 1 | Backtest type | **(a) Decision replay** — re-simulate *stored* `llm_decisions` against the price path that followed. Deterministic, **zero LLM calls**. (LLM replay = non-deterministic + expensive on the local 27B model; deferred.) |
| 2 | Backtest price history | **Fresh historical candles** from the source (Kraken via CCXT / yfinance) for arbitrary date ranges — the agent does not run 24/7, so stored `market_snapshots` alone is too sparse. Stored snapshots are kept as a secondary/audit source. |
| 3 | Dashboard control scope | **Pause/resume** + **close all open positions** + **safe config management** (see #6). No manual order placement, no live risk-param override, no kill in v1. |
| 4 | Agent ↔ dashboard control channel | **Agent exposes a small HTTP control API (FastAPI); the dashboard calls it** — real-time control (e.g. "close all" is immediate, not gated on the 5-min cycle). |
| 5 | Dashboard stack | **FastAPI + Jinja2/HTMX** (server-rendered, HTMX for updates + control), lightweight chart lib (uPlot) via CDN for time-series. No Node/npm build step → one slim Docker image. |
| 6 | Config management | Dashboard edits **safe data only** — intervals, pairs/symbols, `risk.*`, `execution.*`, `monitoring.*`, `decision_history_limit`. **Never** `llm.*` credentials/endpoints, never API keys, never `.env`. |
| 7 | Database | **One SQLite (WAL mode) on a shared Docker volume.** Agent = primary writer; dashboard = reader + control writer; backtester = reader. Keeps the existing SQLAlchemy + aiosqlite stack. |
| 8 | Pipeline shape | **Single unified pipeline** (one code path: provider → indicators → store) feeding all three consumers (agent, dashboard, backtester). |

### Data pipeline (one path, three consumers)

```
                 ┌─────────────── MARKET DATA (OHLCV) ───────────────┐
                 │   CCXT → Kraken        xAPI / yfinance → stocks   │
                 └───────────────┬───────────────────────┬────────────┘
                                 ▼                       ▼
        ┌──────────────────────────────────────────────────────────────┐
        │                    UNIFIED PIPELINE                          │
        │  fetch candles → compute indicators → normalize → persist    │
        │  (one code path — src/core/decision_pipeline.py + providers) │
        └───────────────┬───────────────────────┬──────────────────────┘
                        │                       │
        ┌───────────────┘                       └───────────────┐
        ▼                                                       ▼
  ┌──────────────┐   ┌─────────────────────┐         ┌──────────────────────┐
  │  AGENT (live) │   │  BACKTESTER (replay) │         │   DASHBOARD (monitor  │
  │ prompt→LLM→  │   │ stored decisions vs │         │   + control + config)│
  │ risk→execute │   │ historical candles  │         │   FastAPI + HTMX     │
  └──────┬───────┘   └──────────┬──────────┘         └──────────┬───────────┘
         │ writes               │ reads                          │ reads + control writes
         ▼                      ▼                                ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                 SHARED SQLite (WAL) — data/trading_agent.db                  │
  │   market_snapshots · llm_decisions · orders · portfolio_snapshots            │
  │   + NEW: agent_control (pause/resume, close-all, status, config overrides)   │
  └─────────────────────────────────────────────────────────────────────────────┘
```

> The **live agent** and the **backtester** both read the *same* indicator logic and (for backtest) the same decision rows — so what you backtest is exactly what the pipeline produces. The backtester does **not** re-run the LLM; it re-simulates the recorded decisions.

### Storage (single source of truth)

- **One SQLite database** at `config.storage.database_path` (`data/trading_agent.db`), run in **WAL mode** so the dashboard can read while the agent writes, with no lock contention on the shared volume.
- Existing tables (no schema change): `market_snapshots`, `llm_decisions`, `orders`, `portfolio_snapshots`.
- **New table — `agent_control`** (control plane, dashboard read/write):

  | Column | Purpose |
  |---|---|
  | `agent` | `crypto` / `stocks` — one control row per agent |
  | `state` | `running` / `paused` (pause/resume) |
  | `close_all_requested` | boolean latch — agent closes all open positions then clears it |
  | `status` / `last_cycle_at` / `last_error` | live health for the dashboard |
  | `config_override_json` | **safe** config overrides (see #6); empty = use `settings.yaml` |

  The agent checks `agent_control` at the top of every cycle (cheap SQLite read) → respects pause + close-all. This keeps the DB the single source of truth even though the dashboard *triggers* actions via the control API.

### Control API contract (agent-side, FastAPI)

The agent process serves a small internal API (in-process with the loop, or a thin sidecar):

| Method & path | Effect |
|---|---|
| `GET /api/agents` | state, `last_cycle_at`, open positions, recent decisions, `last_error` (per agent) |
| `GET /api/agents/{agent}/decisions?limit=N` | recent decisions + outcomes (net PnL) |
| `GET /api/agents/{agent}/portfolio` | current + historical portfolio value |
| `POST /api/agents/{agent}/pause` | set `state=paused` |
| `POST /api/agents/{agent}/resume` | set `state=running` |
| `POST /api/agents/{agent}/close-all` | set `close_all_requested` latch (immediate on next loop tick) |
| `GET /api/config` | **safe** config (credentials/keys redacted) |
| `PUT /api/config` | validate against Pydantic `Settings`, persist safe overrides to `agent_control`, reload agent |

> **Safety:** `PUT /api/config` only accepts the safe whitelist (#6); unknown/credential keys are rejected. The LLM endpoint, model, and any `.env` secret are **never** read, written, or returned.

### Dashboard (FastAPI + Jinja2/HTMX, Docker)

- **Monitor:** portfolio value over time (uPlot), open positions, recent decisions + win-rate / confidence distribution, agent state + last cycle + errors. Live via HTMX polling (or SSE).
- **Control:** Pause / Resume, Close all — HTMX `POST` to the control API.
- **Config:** server-rendered form over the safe config surface only; changes validated server-side (Pydantic) and persisted to `agent_control`; no credential/secret fields exist in the form.

### Container / volume topology

```
docker-compose
├── agent-crypto     # scripts/run_crypto_agent.py  (control API in-process)
├── agent-stocks     # scripts/run_stocks_agent.py
├── backtester       # scripts/backtest.py          (batch, on-demand)
├── dashboard        # FastAPI + HTMX               (port 8080 → browser)
└── volume: agent-data → data/          # the shared SQLite + WAL files
    volume: agent-config → config/      # settings.yaml + safe overrides
```

- **One shared `agent-data` volume** holds the SQLite DB (agent writes, dashboard/backtester read). WAL mode permits concurrent read/write.
- `agent-config` volume holds `settings.yaml` + safe overrides; the dashboard and agent both mount it.
- No Postgres in v1; revisit only if multi-writer contention shows up (WAL + single primary writer should not).

### Backtester design (decision replay)

`scripts/backtest.py` — deterministic, no LLM:

1. **Ingest** fresh historical candles for the window (per #2) via the *same* providers — or read stored snapshots when they cover the window.
2. **Load** the recorded `llm_decisions` (+ `orders`, realized PnL) in time order.
3. **Re-simulate** each decision against the price path that followed, through the **same** risk engine + fee/slippage model as live, so the verdicts and PnL are comparable to paper results.
4. **Report:** total return vs. buy-and-hold benchmark, win rate, avg win/loss, max drawdown, Sharpe, per-symbol breakdown.

> **Why decision replay (not LLM replay):** it is deterministic, free, and tests the parts we control (risk engine, execution, fees) against real price paths. LLM replay (feeding history back to the model for *fresh* signals) is a separate, later experiment — non-deterministic and costly on the local 27B model.

### Dependencies added (Week 6)

```toml
# pyproject.toml
fastapi>=0.110        # agent control API + dashboard
uvicorn[standard]     # ASGI server for both
jinja2>=3.1           # server-rendered templates
# htmx + uPlot are CDN assets (no pip dependency)
# Docker: python:3.11-slim image, docker-compose for the topology above
```

### Test additions (Week 6)

- `tests/unit/test_control_api.py` — pause/resume/close-all/config round-trips; config whitelist rejects credentials.
- `tests/unit/test_backtest.py` — replay math (return, win-rate, max DD, Sharpe) on synthetic candles; benchmark comparison.
- `tests/integration/test_dashboard.py` — dashboard reads a seeded SQLite; control endpoints drive the `agent_control` table.
- Property-based: "close-all leaves no open positions"; "config override never contains a key/credential."

---

## Phase 1 — Foundation (Weeks 1-2)

### 1.1 Project Scaffolding

```
trading_agent/
├── pyproject.toml
├── config/
│   └── settings.yaml              # Global config (LLM, schedules, limits)
├── .env.example                   # API keys, LLM endpoint (copy to .env)
├── src/
│   ├── core/                      # Shared infrastructure
│   │   ├── llm_client.py          # LM Studio HTTP client
│   │   ├── risk_engine.py         # Hard-coded risk rules
│   │   ├── storage.py             # SQLite models + queries
│   │   ├── scheduler.py           # APScheduler wrapper
│   │   └── decision_pipeline.py   # Data → features → LLM → signal → order
│   ├── agents/                    # Per-market agents
│   │   ├── crypto_agent.py        # Kraken agent
│   │   └── stocks_agent.py        # XTB agent
│   ├── data/                      # Market data providers
│   │   ├── ccxt_provider.py       # Crypto OHLCV via CCXT
│   │   └── xtb_provider.py        # Stocks OHLCV via xAPI + yfinance fallback
│   │   # planned: sentiment_provider.py (news/social), economic-calendar feed
│   ├── execution/                 # Order placement
│   │   ├── kraken_executor.py     # Kraken testnet orders
│   │   ├── xtb_executor.py        # XTB demo orders
│   │   └── paper_executor.py      # Pure simulation fallback
│   ├── analysis/                  # (reserved for future split-out modules)
│   │   # NOTE: indicators + prompt building currently live in core/decision_pipeline.py
│   │   # planned: indicators.py, prompt_builder.py
│   └── monitoring/                # Observability
│       ├── logger.py              # Structured logging
│       └── alerts.py              # Alert dispatch (logging sink)
├── tests/
│   ├── unit/                      # one test module per source module
│   │   ├── test_llm_client.py, test_risk_engine.py, test_storage.py, test_config.py
│   │   ├── test_decision_pipeline.py, test_ccxt_provider.py, test_xtb_provider.py
│   │   └── test_kraken_executor.py, test_xtb_executor.py, test_paper_executor.py
│   │       test_stocks_market_hours.py, test_scheduler.py, test_monitoring.py, test_models.py
│   ├── integration/
│   │   ├── test_crypto_agent.py
│   │   └── test_stocks_agent.py
│   └── conftest.py                # Fixtures, mocks
├── scripts/
│   ├── run_crypto_agent.py        # Entry point: crypto agent
│   ├── run_stocks_agent.py        # Entry point: stocks agent
│   └── backtest.py                # (planned — Week 6) Replay historical decisions
├── data/                          # Local cache for market snapshots
└── docs/
    └── API_NOTES.md               # Kraken + XTB API quirks
```

### 1.2 Shared Core Implementation

| Module | Responsibility | Tests |
|---|---|---|
| `llm_client.py` | HTTP client to LM Studio (`localhost:1234`). Sends prompts, parses structured JSON response with retry logic and timeout handling. | Mock server tests. Verify prompt formatting, JSON parsing, retry on failure, fallback signal on LLM error. |
| `risk_engine.py` | Deterministic gate before every order. Checks: max position size (% of portfolio), daily loss limit, stop-loss distance, cooldown after consecutive losses, max open positions. Returns `Approved` / `Rejected(reason)`. | Test every rule in isolation and combined. Verify rejection with correct reason strings. Edge cases: zero balance, negative PnL accumulation. |
| `storage.py` | SQLite via SQLAlchemy. Tables: `market_snapshots`, `llm_decisions`, `orders`, `portfolio_state`. Time-indexed for backtesting replay. | Test CRUD on every table. Verify time-series queries. Test schema migrations. |
| `scheduler.py` | APScheduler wrapper. Configurable intervals per agent (e.g., crypto: 5min, stocks: 15min during market hours). Graceful shutdown. | Test job registration, interval accuracy, graceful stop, error isolation between jobs. |
| `decision_pipeline.py` | Orchestrates the full cycle: fetch data → compute features → build prompt → call LLM → parse signal → risk check → execute/store. | Mock every stage. Test happy path and failure at each step (data fetch fails, LLM times out, risk rejects). Verify nothing executes without risk approval. |

### 1.3 Data Providers

| Provider | Source | What it provides | Status |
|---|---|---|---|
| `ccxt_provider.py` | CCXT library → Kraken | OHLCV candles → `MarketSnapshot` | ✅ built |
| `xtb_provider.py` | xAPI Python SDK + yfinance fallback | OHLCV candles → `MarketSnapshot` | ✅ built |
| `sentiment_provider.py` | news / social scrapers | Sentiment signal | ⏳ planned |
| economic calendar | CPI / rate-decision feed | Event context for the stocks prompt | ⏳ planned |

### 1.4 Execution Adapters

All executors implement the same `Executor` Protocol (defined in `src/core/models.py`):

```python
class Executor(Protocol):
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> OrderResult: ...
    async def get_positions(self) -> list[Position]: ...
    async def cancel_order(self, order_id: str) -> bool: ...
    async def get_cash(self) -> float: ...
```

- `kraken_executor.py` — Kraken testnet REST API. Maps signals to Kraken order types (market/limit/stop).
- `xtb_executor.py` — xAPI demo trading. Maps signals to XTB order format.
- `paper_executor.py` — Pure simulation. No network calls. Tracks virtual portfolio state. **Default for all testing.**

---

## Phase 2 — Agents (Weeks 3-4)

### 2.1 Crypto Agent (`agents/crypto_agent.py`)

**Cycle:** Every 5 minutes, 24/7.

1. Fetch OHLCV for configured pairs via CCXT ✅
2. Compute indicators: RSI, MACD, Bollinger Bands ✅ (inline in `core/decision_pipeline.py`; VWAP / volume profile — *planned*)
3. ~~Fetch recent news/sentiment~~ — ⏳ *not yet implemented* (no sentiment provider)
4. Build prompt with portfolio state + market data ✅; **recent-decisions context — ✅ wired** (last N prior decisions injected under `CONTEXT:`, config `decision_history_limit`; **outcome/PnL — ✅ wired**, see §7.20)
5. Call LLM → structured signal ✅
6. Risk engine gate ✅
7. Execute on Kraken testnet or paper mode ✅
8. Store everything ✅

**Prompt design (target):** Include the last N decisions and their outcomes so the LLM can learn from its own track record. **Done ✅** — `build_user_prompt()` fetches the last N prior decisions via `get_recent_decisions()` and renders them under `CONTEXT:` (action, confidence, reasoning, risk verdict, timestamp), **and the *outcome* half is now wired** — each decision shows its net realized PnL (win/loss/still-open), net of fees, so the LLM learns not only what it decided but whether it paid off (see §7.20).

### 2.2 Stocks Agent (`agents/stocks_agent.py`)

**Cycle:** Every 15 minutes during market hours (Warsaw exchange: 9:00-16:30 CET).

1. Fetch OHLCV for configured symbols via xAPI + yfinance ✅ (in `xtb_provider.py`)
2. ~~Economic-calendar events~~ — ⏳ *not yet implemented*
3. Compute indicators (RSI, MACD, Bollinger Bands) ✅
4. Build prompt with portfolio state + market data ✅
5. Call LLM → structured signal ✅
6. Risk engine gate ✅
7. Execute on XTB demo (OAuth2 flow ⏳ *planned*) or paper mode ✅
8. Market-hours guard (WSE 09:00–16:30) ✅
9. Store everything ✅

### 2.3 Prompt Engineering (`analysis/prompt_builder.py`)

The prompt template is the most important piece — it determines how well the LLM reasons about trades. Structure:

```
SYSTEM: You are a quantitative trading analyst. Analyze market data and produce 
a structured trading signal. Be conservative. When uncertain, recommend HOLD.

CONTEXT:
- Portfolio state (positions, cash, PnL)   ✅ implemented
- Recent decisions and their outcomes (last 10)   ✅ actions/verdicts **and** realized-PnL outcome wired (see §7.20)
- Market conditions summary

DATA: [symbol] {timeframe}
- Price action: OHLCV summary
- Technical indicators: RSI, MACD, BB, etc.
- Volume analysis
- Sentiment/news (if available)

RULES:
- Return ONLY valid JSON matching the schema
- Confidence must reflect genuine uncertainty
- Stop-loss must be technically justified
- Never recommend more than one action per symbol

SCHEMA: {json_schema}
```

---

## Phase 3 — Safety & Monitoring (Week 5)

### 3.1 Circuit Breakers

Hard-coded, non-negotiable gates in `risk_engine.py` (built Week 2 ✅). `RiskEngine.evaluate()` runs all of these on every signal, in order:

| Rule | Default | Configurable | Implementation |
|---|---|---|---|
| Min confidence | `0.6` to trade | Yes | `_check_confidence` |
| Max open positions | 5 per agent | Yes | `_check_max_positions` |
| Max position size | 10% of portfolio per symbol | Yes | `_check_position_size` |
| Daily loss limit | -2% of starting balance | Yes | `_check_daily_loss` |
| Max drawdown | -5% vs starting capital | Yes | `_check_drawdown` *(simplified — compares to starting capital, not peak)* |
| Consecutive-losses cooldown | 3 losses → 60-min pause | Yes | `_check_cooldown` |
| Stop-loss required | BUY/SELL must include a stop-loss | No | `_check_stop_loss` |

> The earlier "-5% drawdown → halt for 24h" phrasing is not how the code behaves: there is no time-based halt. The 24-hour-scale protection is the **consecutive-losses cooldown** (3 losses → 60 min, configurable via `consecutive_losses_cooldown_minutes`).

### 3.2 Monitoring

- Structured JSON logs for every decision (timestamp, symbol, signal, reasoning, risk verdict, execution result) ✅ `monitoring/logger.py`
- Alert dispatch on trades and risk rejections ✅ `monitoring/alerts.py`
- Web dashboard (FastAPI + Jinja2/HTMX, Docker) — monitoring (portfolio value over time, recent decisions, LLM confidence distribution, win rate) **plus control** (pause/resume, close-all) **and safe config management** — ⏳ *planned (Week 6)*. Full design: §"Data Pipeline, Storage & Dashboard (Week 6)" and §7.15.

### 3.3 Backtesting (`scripts/backtest.py`) — ⏳ planned (Week 6)

**Decision replay** (locked): re-simulate the *stored* `llm_decisions` (+ `orders`, realized PnL) against the price path that followed, through the **same** risk engine + fee/slippage model as live — deterministic, **zero LLM calls**. Price history comes from **fresh historical candles** (Kraken via CCXT / yfinance) for arbitrary date ranges, since the agent does not run 24/7; stored `market_snapshots` are a secondary/audit source.

Metrics:
- Total return vs. buy-and-hold benchmark
- Win rate, average win/loss ratio
- Max drawdown, Sharpe ratio
- Per-symbol performance breakdown

> LLM *replay* (feeding history to the model for fresh signals) is a separate, later experiment — non-deterministic and costly on the local 27B model. See §"Data Pipeline, Storage & Dashboard (Week 6)".

---

## Phase 4 — Iteration & Improvement (Ongoing)

### 4.1 LLM Fine-Tuning Loop

1. Run paper trading for 2-4 weeks
2. Export all decisions + outcomes to a dataset
3. Identify patterns: when did the LLM make good calls vs. bad ones?
4. Refine prompts based on failure modes
5. Consider fine-tuning if running a local model that supports it

### 4.2 Strategy Expansion

- Add more indicators (orderbook imbalance, funding rates for crypto)
- Multi-timeframe analysis (LLM evaluates signals across timeframes)
- Correlation analysis between assets to avoid concentrated risk
- Regime detection (trending vs. ranging markets → different strategies)

### 4.3 Live Trading Readiness Checklist

- [ ] Paper trading PnL positive for ≥ 4 weeks
- [ ] Win rate > 50% after fees simulation
- [ ] Max drawdown within acceptable bounds
- [ ] LLM response time consistently < timeout threshold
- [ ] All circuit breakers tested and verified
- [ ] Exchange API rate limits understood and respected
- [ ] Disaster recovery plan (network outage, exchange downtime)

---

## LM Studio Integration Details

**Endpoint:** `http://127.0.0.1:1234/v1/chat/completions` (OpenAI-compatible)

**Model:** Qwen 3.8 27B (`qwen/qwen3.8-27b`)

**Key considerations:**
- Use JSON mode / structured output if the model supports it, otherwise validate and retry on parse failure
- Keep prompts under context window — trim old data aggressively
- Set reasonable timeout (15-30s) with fallback to HOLD signal on LLM failure
- Log full prompt + response for auditability

---

## API Notes

### Kraken Testnet
- Base URL: `https://demo.kraken.com` (or use CCXT's testnet flag)
- Auth: API key + secret via HMAC-SHA256 signatures
- Rate limits: Check current docs — implement exponential backoff
- Order types: market, limit, stop-loss, take-profit supported

### XTB Demo
- xAPI requires registration and approval for API access (demo is easier)
- Python SDK available: `xtb-api` or REST via `httpx`
- Auth: OAuth2 flow — store tokens securely
- Trading hours: Warsaw Stock Exchange schedule
- Instruments: Stocks, CFDs, indices

---

## Testing Strategy

### Unit Tests (fast, no network)
- Every pure function and class method
- Mock all external dependencies (LLM client, exchange APIs, database)
- Target: >90% coverage on `core/` modules

### Integration Tests (mocked network)
- Full decision pipeline with mocked data provider + paper executor
- Agent lifecycle: start → cycle → shutdown
- Risk engine integration with realistic portfolio states

### Property-Based Tests
- Risk engine invariants: "approved signal always satisfies all rules"
- Storage consistency: "every executed order has a matching decision record"

### Test Data
- Realistic OHLCV fixtures from historical data
- Edge cases: gap-ups, zero volume, extreme volatility periods

---

## Configuration (`config/settings.yaml`)

```yaml
llm:
  endpoint: "http://127.0.0.1:1234/v1/chat/completions"
  model: "qwen/qwen3.8-27b"
  timeout_seconds: 30
  max_retries: 3
  use_json_schema: false    # opt-in strict JSON mode; enable once the local model accepts response_format

crypto_agent:
  enabled: true
  exchange: kraken
  testnet: true
  interval_minutes: 5
  pairs:
    - BTC/USDT
    - ETH/USDT
  decision_history_limit: 10   # prior decisions fed back into the prompt (0 = off)

stocks_agent:
  enabled: false          # Enable after crypto agent is stable
  broker: xtb
  demo: true
  interval_minutes: 15
  market_hours: "09:00-16:30"   # CET
  symbols:
    - AAPL
    - MSFT
  decision_history_limit: 10   # prior decisions fed back into the prompt (0 = off)

risk:
  max_position_pct: 0.10
  daily_loss_limit_pct: 0.02
  max_drawdown_pct: 0.05
  consecutive_losses_cooldown_minutes: 60
  max_open_positions: 5
  min_confidence: 0.6

# Paper-executor costs so realized PnL (and the LLM's feedback loop) is net of
# fees/slippage. 0.26%/side matches a typical crypto taker fee; 0.1%/side slippage.
execution:
  paper_fee_pct: 0.0026
  paper_slippage_pct: 0.001

storage:
  database_path: "data/trading_agent.db"

monitoring:
  log_level: INFO
  alert_dedup_window_seconds: 300
```

> `crypto_agent.watchlist_size` (an earlier draft) is **not** present in the real config and not consumed by any code — dropped. The authoritative config is `config/settings.yaml`; `Settings` in `src/core/config.py` validates it.

---

## Dependencies (Initial)

```toml
# pyproject.toml (current)
dependencies = [
    "ccxt>=4.0",           # Crypto exchange unified API
    "pandas-ta>=0.3",      # Technical analysis indicators
    "httpx>=0.27",         # Async HTTP for LLM + APIs
    "sqlalchemy>=2.0",     # Database ORM
    "aiosqlite>=0.20",     # Async SQLite driver
    "apscheduler>=3.10",   # Job scheduling
    "pydantic>=2.0",       # Data validation (signals, configs)
    "python-dotenv>=1.0",  # Environment variables
    "pyyaml>=6.0",         # Config parsing
    "structlog>=24.0",     # Structured logging
]

[project.optional-dependencies]
stocks = ["yfinance>=0.2"]   # optional; stocks data fallback (pulls in pandas)

# dev: pytest, pytest-asyncio (auto mode), pytest-cov, hypothesis
#      (see the actual pyproject.toml + AGENTS.md for the canonical list)
```

---

## Implementation Order

1. **Week 1:** `pyproject.toml`, config, `llm_client.py`, `storage.py` + tests ✅
2. **Week 2:** `risk_engine.py`, `decision_pipeline.py`, `paper_executor.py` + tests ✅
3. **Week 3:** `ccxt_provider.py`, `kraken_executor.py`, `crypto_agent.py`, `scheduler.py`, `run_crypto_agent.py` + integration tests ✅
4. **Week 4:** Monitoring (structured logging) ✅; **decision-history prompt wiring** ✅; **crypto agent paper mode on real data** ✅ (the paper path now fetches live public Kraken OHLCV — no API key required — and executes via the fee/slippage-aware `PaperExecutor`; Kraken testnet execution remains opt-in via `KRAKEN_API_KEY`)
5. **Week 5:** `xtb_provider.py`, `xtb_executor.py`, `stocks_agent.py` + tests ✅ (68 new tests added)
6. **Week 6:** Data-pipeline + Docker dashboard (FastAPI + HTMX: monitor / control / safe config), **decision-replay backtesting** on fresh historical candles, alerting ◄ **(Next)** — see §"Data Pipeline, Storage & Dashboard (Week 6)" and §7.15 (phased)

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LLM gives bad signals | Financial loss (even paper) | Risk engine gates everything; conservative defaults |
| LLM is too slow | Missed trading opportunities | Timeout with HOLD fallback; optimize prompt size |
| Exchange API rate limits | Data gaps, failed orders | Rate limiting built in; exponential backoff |
| XTB API access delayed | Stocks agent blocked | Start with crypto only; use yfinance for data even without execution |
| Overfitting to paper trading | Live performance differs | Simulate fees/slippage; start small if going live |
| Hallucinated indicators | Wrong decisions | Validate LLM output against computed values; include raw numbers in prompt |
| LLM non-determinism | Backtest replay won't reproduce stored decisions | `temperature=0.2` set (not 0); **no seed** — pin `temperature=0` and a seed where the model allows, and log the full prompt+response for audit (still open) |
| Paper PnL is optimistic | Overstates strategy quality | Verify `PaperExecutor` fee/slippage defaults before trusting paper PnL against the §4.3 live-readiness gates |

---

## 7. Gaps & Next Steps

Updated after the full-codebase review of **2026-09-15** — findings are tagged **[R-xx]** referencing `review.MD` at the repo root (H = high, M = medium, L = low severity there). Ordering is now: **A. low-hanging fruit first**, then the rest by severity (**B. High → C. Medium → D. Low/housekeeping**); completed work sits at the end (**E**) for the record. Items marked ⏳ are referenced as *planned/not yet implemented* throughout this plan.

### A. Low-hanging fruit (hours each — do first)

1. **Refresh paper-position prices every cycle** — ✅ **complete** **[R-H1]**
   - **Done ✅:** `DecisionPipeline.run` now calls the new `_mark_positions(symbol, snapshot)` immediately after the fetch step and **before** the risk check: it feeds the snapshot's last close into the optional executor hook `update_price(symbol, close)`, which `PaperExecutor` implements (no-op for symbols with no open position). Real-venue executors (Kraken/XTB) report live prices and don't implement the hook, so they're skipped via duck-typing; marking is fail-soft (a failure logs a warning, never breaks the cycle). Result: `unrealized_pnl` moves with the market, `_get_portfolio_state()` (risk gate + the agent's `update_daily_value` baseline) sees total_value at market — making the `-daily_loss_limit_pct: 0.02` guard honest in paper mode — and persisted portfolio snapshots carry current marks. Covered by `TestPositionMarking` in `tests/unit/test_decision_pipeline.py` (re-mark to last close with `unrealized_pnl != 0`; risk check receives the market-valued portfolio; no phantom positions; hook-less executors unaffected). 233 tests passing.
   - **Side effect found while testing:** sell sizing (`max_position_pct × total_value`) is not clamped to units held, so a marked-to-market total can now produce a sell slice larger than the position → rejected order. Pre-existing behavior (tracked in §7.19's float/sizing nits), surfaced not caused by this fix.

2. **Fix `enabled: false` semantics** — ✅ **complete** **[R-H3]**
   - **Was:** both runners treated "disabled" as *run one full cycle — including order placement, LLM calls and DB writes — then exit*. A user who disabled an agent to be safe still got trades executed.
   - **Done ✅:** both `scripts/run_crypto_agent.py` and `scripts/run_stocks_agent.py` now check `<agent>.enabled` immediately after loading settings/logging and **exit before constructing any component** — no storage init, LLM client, provider/executor, pipeline or agent, so no cycles, orders or DB writes are possible. Single-cycle mode is now an explicit CLI flag: `python -m scripts.run_crypto_agent --once` (same for stocks) runs exactly one full cycle with a guaranteed clean shutdown (`agent.shutdown` + provider/executor/storage closes via `try/finally`; a failing cycle propagates so cron sees a non-zero exit) and never starts the scheduler. Verified live: running the disabled stocks agent logs the warning and exits 0 without touching the DB.
   - **Tests:** new `TestEnabledSemantics` in `tests/unit/test_run_crypto_agent.py` (disabled → storage/LLM/pipeline/agent factories never called; `--once` → exactly one cycle + closes + scheduler not started; failed `--once` cycle still releases resources) and a new mirror file `tests/unit/test_run_stocks_agent.py` (the stocks runner previously had no tests at all). 239 tests passing.

3. **Dependency & test-claims cleanup** — ✅ **complete** **[R-M4]**
   - **Was:** `pandas-ta` and `python-dotenv` declared in `pyproject.toml` but never imported (indicators are hand-rolled; `.env` loading is a dependency-free `_load_dotenv()`); no dev-dependency group; `AGENTS.md` claimed `hypothesis` + `responses` property tests that didn't exist; the stocks runner logged fetch errors every cycle when `yfinance` was missing.
   - **Done ✅:** removed `pandas-ta` and `python-dotenv` from dependencies (verified nothing imports them); added `[project.optional-dependencies].dev` (`pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `ruff`) so `pip install -e ".[dev]"` reproduces the test env; installed `hypothesis`; added real property-based tests for risk-engine invariants in `tests/unit/test_risk_engine_properties.py` (HOLD always approved; approved active signals carry a stop; sub-minimum confidence rejected; opening beyond `max_open_positions` rejected; breached daily-loss blocks active trades; 3 consecutive losses ⇒ cooldown; deterministic verdicts). `AGENTS.md` corrected — `responses` dropped from the claim (it mocks `requests`, unused here), dev group documented.
   - **Also:** `create_xtb_provider` now checks for `yfinance` eagerly and both it + the stocks runner fail fast with an actionable install hint (mirrors the ccxt one) instead of erroring every cycle; new `TestYFinanceFailFast` covers it. 247 tests passing.

4. **Docs/reality mismatches** — ✅ **complete** **[R-L]**
   - **Done ✅:** `.env.example` — one now lives at the **repo root** (where both runners load `.env` from), with all keys commented out/blank and the XTB + JSON-schema opt-in entries merged in; the divergent duplicate `config/.env.example` was deleted. (`AGENTS.md` docs path corrected `doc/` → `docs/`.)
   - **Done ✅:** README no longer claims "7 deterministic risk rules" while two are stubs — the layout comment now reads *5 live rules; drawdown + notional cap land in §7.5* (restored to 7 once §7.5 lands). Test counts refreshed.

### B. High severity

5. **Close the two no-op risk rules (drawdown; position size at the gate)** — ✅ **complete** **[R-H2]**
   - **Done ✅ (drawdown, option b):** `RiskEngine._check_drawdown` is live: it tracks a high-water mark (`note_equity`, lazily seeded from the first reading) and rejects any active signal while equity is more than `risk.max_drawdown_pct` below the peak. Cross-restart persistence uses **SQLite**: `Storage.get_max_portfolio_value()` (MAX over `portfolio_snapshots.total_value`) seeds the engine at startup in both runners (`seed_peak_equity`, fail-soft), so a restart can't reset the guard. The engine stays a pure sync object — the peak is fed in from storage by the caller, mirroring `update_daily_value`.
   - **Done ✅ (size at the gate):** the pipeline now computes the planned quantity *before* the risk check and passes `planned_notional = quantity × price` into `evaluate()`; `_check_position_size` rejects when it exceeds `max_position_pct × total_value` (tiny float tolerance), and the approved plan is reused unchanged at execution — a sizing regression can no longer slip past approval. Sell sizing is additionally clamped to units actually held, which kills the oversized-sell → guaranteed-rejection loop surfaced as §7.1's side effect.
   - **Tests:** `TestDrawdown` + `TestPositionSizeGate` (`test_risk_engine.py`), `get_max_portfolio_value` pair (`test_storage.py`), `TestSizingAtTheGate` — gate receives the notional, an oversized plan is rejected end-to-end with a real engine, sells clamp to held units (`test_decision_pipeline.py`). 260 tests passing.
   - **Was:** `_check_drawdown` returned `APPROVED` unconditionally (config value unused), and `_check_position_size` only guarded total value ≤ 0 — actual sizing happened after approval with nothing validating it.

6. **Real-CCXT integration pass for the keyed Kraken path** — ✅ **code pass complete; live-testnet smoke still pending** **[R-H4]**
   - **Done ✅:** `get_cash` now parses real ccxt `fetch_free_balance` payloads (currency→`{free,used,total}` dicts, case-insensitive quote lookup, missing quote → 0.0, bare-float stubs still work) instead of `float(balance)` on a dict; closed orders record the **fill average price** and `filled_at` from ccxt's ms timestamps (`updated`/`closedAt`/`timestamp`), with filled-quantity preference over requested; `get_positions` survives Kraken-spot's `fetch_positions` rejection (warns once, returns `[]`) instead of raising every keyed cycle. Documented the remaining venue limitations in the module docstring. Tests: `TestRealCcxtShapes` uses payloads shaped like recorded ccxt 4.5.x responses (266 tests passing).
   - **Still open:** a live keyed smoke run against the Kraken testnet from a network-enabled environment (sandbox blocks outbound HTTPS — see `nightly_finds.md` #1), and per-cycle reconciliation of orders left `open` (marketable limits at last close usually return `closed` in `create_order`, so this is now rare but unpinned).

7. **Persist / rehydrate paper portfolio + risk state across restarts** — ✅ **complete** **[R-M1]**
   - **Done ✅:** new `src/core/rehydration.py` with one fail-soft startup call (`rehydrate_from_storage`) wired into both runners before any cycle: the paper book is restored from the latest `portfolio_snapshots` row via a new `PaperExecutor.load_portfolio_state(cash, positions)` hook (live-venue executors lack the hook → skipped); the daily-loss baseline is rehydrated from today's *earliest* snapshot (`get_first_portfolio_snapshot_of_day`); and the losing streak + still-running cooldown are rebuilt from trailing negative outcomes in closed decisions (`get_closed_decisions`, newest-first walk; cooldown restarts from the newest loss's timestamp + `consecutive_losses_cooldown_minutes`).
   - **Done ✅:** `initial_cash` is config-driven — new `execution.initial_cash` (default 100 000) seeds a *fresh* paper portfolio only; after the first cycle the persisted snapshot wins. Both runners pass it to `PaperExecutor`.
   - **Tests:** `tests/unit/test_rehydration.py` (cash/positions/marks restored, no-snapshot and hook-less skips, baseline honesty after restart, streak+cooldown restored, recent win breaks streak, combined entry point) plus new storage query tests. 276 tests passing.

### C. Medium severity

8. **Decision-history quality: attribute outcomes to entry decisions; exclude fallback rows** — ✅ **complete** **[R-M2/M3]** *(absorbs the earlier external-review item "Deferred #4")*
   - **Done ✅ (shared FIFO tracker):** new `src/execution/position_tracker.py` (`PositionTracker` + `_Lot`/`SellOutcome`) is fed by **all three executors**, so closing fills now report `OrderResult.realized_pnl` everywhere — Kraken and XTB previously left it `None`, which silently no-oped the whole "learn from your track record" backfill on real venues. Lots carry the entry `decision_id`, and a sell returns `closed_entries: list[ClosedEntry]` attributing net PnL per originating decision (buy-side commission pro-rated per lot, sell-side fee pro-rated across the fill). Paper keeps exact fee honesty (single-lot FIFO reproduces the old average-cost numbers); venue fills are **gross of commission** because `create_order` payloads report none — documented in both executors. A venue sell with nothing locally tracked (holdings opened before a restart) reports *no* outcome rather than a fabricated break-even one.
   - **Done ✅ (outcomes reach the entry row):** `Executor.place_order` gained a `decision_id` kwarg and the pipeline now persists each decision **itself, immediately after the risk gate** (`DecisionPipeline._persist_decision`, fail-soft; agents lost their duplicate `_persist_decision`), so the id exists *before* the fill. Agents backfill via the new `Storage.add_realized_pnl(decision_id, delta)` (COALESCE-sum, so multi-tranche closes accumulate) over `order_result.closed_entries`, while the sell row keeps its own `set_realized_pnl` stamp.
   - **Done ✅ (fallback rows quarantined):** `TradeSignal.is_fallback` (never forgeable — `_parse_signal` strips it from model output) → new `llm_decisions.is_fallback` column (added by the existing ALTER-TABLE migration), set by the LLM client's exhausted-retries HOLD; `get_recent_decisions` filters them out, so they stay in the DB for audit but never re-enter prompt context.
   - **Done ✅ (audit logging, §3.3):** `llm_client.py` moved to structlog and logs a full `llm_exchange` event (system prompt + user prompt + raw response) per live decision; retries log `llm_attempt_failed` / `llm_retries_exhausted`.
   - **Tests:** new `tests/unit/test_position_tracker.py` (FIFO order, per-entry aggregation, pro-rated fees both sides, partial consumption, dust pop); attribution classes in `test_paper_executor.py` (incl. rehydrated-book cost basis), `test_kraken_executor.py`, `test_xtb_executor.py` (untracked/pending/unpriced sells report nothing); `test_storage.py::TestDecisionAttribution`; fallback-flag tests in `test_llm_client.py`; `TestDecisionPersistence` in `test_decision_pipeline.py`. Both integration suites now drive a **real** `DecisionPipeline` (only provider + LLM mocked) and pin the two-cycle buy→sell story: order→decision links, sell-row stamping, entry-row backfill. **309 tests passing, 94% coverage.**
   - **Real-venue realized PnL never backfilled (feedback loop skipped for Kraken/XTB):** `PaperExecutor` computes net `realized_pnl` on sells (average-cost), but `KrakenExecutor` / `XTBExecutor` never set `realized_pnl`, and both agents gate the backfill on `order_result.realized_pnl is not None` — so the "learn from its track record" loop silently no-ops on real venues. **Decision needed before building:** a shared cost-basis **`PositionTracker`** (FIFO vs LIFO — recommend **FIFO**) used by **all** executors so `OrderResult.realized_pnl` is populated on closing fills, unifying paper (currently average-cost) and real venues. It must actually set `OrderResult.realized_pnl` on closing fills or the agent gate still won't fire; then add executor tests asserting `realized_pnl` on a closing sell.
   - **Outcomes land on the wrong row:** the realized PnL of a closing sell is stamped onto *the sell's own decision row*, while the originating buy row stays `outcome: still open` forever — the LLM never sees how its entry decisions turned out. Use `orders.decision_id` + the `PositionTracker` above to backfill the opening decision when its position closes.
   - **LLM-fallback rows pollute context:** after exhausted retries, the HOLD fallback (reasoning "LLM unavailable…") is persisted as a normal *approved* decision and re-fed into subsequent prompts as one of the last-N. Mark fallback decisions (dedicated column or verdict value) and exclude them from `get_recent_decisions`. Also close PLAN's own audit requirement (§3.3 note): log full prompt+response for live decisions — today only `signal.action` is logged.

9. **Deterministic stop-loss / take-profit enforcement** ⏳ **[R-M5]** — signals carry `stop_loss`/`take_profit` and the risk gate *requires* a stop on any active signal, but nothing ever monitors open positions against those levels; in paper mode losing positions exit only if the LLM happens to sell. A per-cycle exit check (close when mark price crosses SL/TP through the executor) bounds drawdown while §7.5 matures and materially improves PnL realism. Depends on §7.1 (prices must be current for exits to trigger).

10. **Weekend/holiday awareness in the stocks market-hours guard** ⏳ **[R-M6]** — `is_market_open` checks time-of-day only; Saturday 10:00 Warsaw runs the agent on Friday's stale daily candles. Add at least a weekday check (holiday calendar later). Config trap while here: an overnight window (`22:00-08:00`) makes `start <= t <= end` never true — the agent silently never runs; fail loudly or handle wrap-around.

11. **Stocks data depth silently disables indicators** ⏳ **[R-M7]** — `YFinanceSource._PERIOD_MAP` requests `"1mo"` of daily bars ≈ 21 closes, below MACD's 26-close minimum, so the stocks prompt often lacks MACD entirely while crypto (100 candles) has it. Request ~`"6mo"`. Also `_f()` maps NaN→0.0, so a row with a missing Low becomes a zero-low candle that poisons ATR/BB — dropping the row is safer.

12. **Storage retention / pruning** ⏳ **[R-M8]** — every cycle persists a full snapshot (~100 candles as JSON) per symbol; at the configured 5-min interval × 2 pairs that's ~58k snapshot rows/day (~30–60 MB/day), and decisions/orders/portfolio snapshots grow unbounded. The Week-6 dashboard/backtester assume this DB stays usable — add retention config or a pruning job before it becomes operational debt.

13. **Deduplicate agents + runners** ⏳ **[R-M9]** — `crypto_agent.py` and `stocks_agent.py` are ~90% identical (cycle loop, post-process, persistence, alerts); both scripts duplicate `_load_dotenv`, `_build_alerts` and ~80 lines of wiring; only the market-hours guard is genuinely agent-specific. Extract a shared base agent + runner factory — every fix currently lands twice (coverage drift already visible: the stocks agent carries extra uncovered branches).

14. **Backtesting (`scripts/backtest.py`)** ⏳ (Week 6) — design in §"Data Pipeline, Storage & Dashboard (Week 6)"
   - **Type (locked): decision replay** — re-simulate the *stored* `llm_decisions` (+ `orders`, realized PnL) against the price path that followed, through the **same** risk engine + fee/slippage model as live. **Deterministic, zero LLM calls.** (LLM replay — feeding history to the model for fresh signals — is a separate, later experiment: non-deterministic + costly on the local 27B model.)
   - **Price history (locked): fresh historical candles** from the source (Kraken via CCXT / yfinance) for arbitrary date ranges — the agent doesn't run 24/7, so stored `market_snapshots` alone is too sparse; stored snapshots remain a secondary/audit source.
   - **Metrics:** total return vs. buy-and-hold benchmark, win rate, avg win/loss, max drawdown, Sharpe, per-symbol breakdown.
   - **Phased build:** (1) historical-candle ingestion for a window (reuse providers); (2) decision-replay engine reusing risk engine + `PaperExecutor` fee/slippage; (3) metrics + report (CLI + JSON); (4) tests on synthetic data (return, win-rate, max DD, Sharpe; benchmark comparison).
   - **Note on LLM non-determinism (Risks):** moot for decision replay (we reuse *stored* decisions, not model calls); still log full prompt+response for the live agent's audit (tracked in §7.8).

15. **Dashboard + control (Docker WebUI)** ⏳ (Week 6) — design in §"Data Pipeline, Storage & Dashboard (Week 6)"
   - **Stack (locked):** FastAPI + Jinja2/HTMX; uPlot charts via CDN; one slim Docker image (no Node build). Monitor + control + safe config.
   - **Control scope (locked):** Pause/Resume, Close-all, and a **safe config editor** (intervals, pairs/symbols, `risk.*`, `execution.*`, `monitoring.*`, `decision_history_limit`). **No** manual order placement, no live risk-param override, no kill. **No credentials/keys** — never read, written, or returned.
   - **Control channel (locked):** agent serves a small FastAPI control API; dashboard calls it (real-time, not gated on the 5-min cycle). DB remains the single source of truth via the new `agent_control` table.
   - **Phased build:**
     - P1 — shared state: enable SQLite WAL; add `agent_control` table + repository methods; agent checks pause/close-all each cycle. (Tests: control round-trips.)
     - P2 — control API: FastAPI endpoints (status, decisions, portfolio, pause/resume, close-all) on the agent process. (Tests: API contract + config whitelist.)
     - P3 — dashboard (monitor): FastAPI + Jinja2/HTMX pages — portfolio history (uPlot), positions, decisions + win-rate/confidence, agent health. (Tests: seeded-DB render.)
     - P4 — dashboard (control + config): Pause/Resume/Close-all buttons; safe config form with server-side Pydantic validation → `agent_control` overrides. (Tests: config whitelist rejects credentials.)
     - P5 — Docker: `Dockerfile` (python:3.11-slim) + `docker-compose.yml` (agent-crypto, agent-stocks, backtester, dashboard) with the shared `agent-data` + `agent-config` volumes.
   - **Acceptance:** from a browser — see live portfolio value/decisions/health; pause & resume the agent and watch it stop/start; close all open positions; change a safe config value and see it take effect on the next cycle. Credentials are never exposed.

16. **XTB demo OAuth2 flow** ⏳
   - `run_stocks_agent.py` currently falls back to the paper executor until the OAuth2 flow lands. Required before real XTB demo trading; the stocks *agent* and *provider* logic is already built and tested against the paper executor.

### D. Low severity / housekeeping

17. **Split `analysis/` out of `core/decision_pipeline.py`** (housekeeping)
   - Indicators (`compute_indicators`, `_compute_rsi`, `_compute_macd`, `_compute_bollinger_bands`) and the default system prompt currently live in `core/decision_pipeline.py`. Move to `analysis/indicators.py` + `analysis/prompt_builder.py` for the plan's intended layout — safe to do now that the prompt work has landed (§7.20).

18. **Data-enrichment feeds** ⏳ (optional, lower priority)
   - Sentiment provider (crypto) and economic-calendar feed (stocks) are still aspirational. Ship the higher-priority items above first; add these as the prompt benefits from richer context.

19. **Code nits bundle** ⏳ **[R-L]** — agents call `pipeline._get_portfolio_state()` (private) in both agents and tests, pinning an encapsulation leak into the test contract; `_check_cooldown` reaches into `_loss_tracker._cooldown_until` with a `# type: ignore`; `DailyLossTracker.daily_portfolio_value -> float` returns `None` via a `getattr` hack; hardcoded values vs the "config-driven" rule: consecutive-loss threshold 3 (`risk_engine.py:67`), LLM `temperature=0.2`/`max_tokens=1024`, paper `initial_cash` (→ §7.7); LLM client retries with **no backoff** and derives base_url via `endpoint.rsplit("/v1", 1)[0]` then re-appends `/v1/chat/completions` — works with the shipped config, fragile otherwise; `risk_engine.py`/`llm_client.py` use stdlib logging while everything else uses structlog (safety-critical rejection logs bypass the configured renderers); `PipelineStep(str)` should be a real `Enum`; sells are sized at `max_position_pct × total_value` regardless of units held → guaranteed-rejected sell loops, and float equality (`pos.quantity == 0`) for position cleanup can strand dust positions that count toward `max_open_positions`; RSI/ATR use simple averages rather than Wilder smoothing — values differ from TradingView/pandas-ta readings, so document them to avoid misreading LLM-facing numbers.

### E. Done (for the record)

20. **Close the "learn from its own track record" loop** — ✅ **complete** *(the plan's core differentiator)*
   - **Done ✅:** `build_user_prompt()` fetches the last N prior decisions via `get_recent_decisions()` and renders them under `CONTEXT:` (action, confidence, reasoning, risk verdict, timestamp); config-driven via `decision_history_limit` (default 10, 0 = off). The LLM now sees *what it decided and why it passed risk*.
   - **Done ✅:** the *outcome* half. `llm_decisions` now has a `realized_pnl` column (idempotent `ALTER TABLE` migration for pre-existing DBs). On a **closing** order (a fill that reports a non-`None` `realized_pnl`), the agent backfills it via `Storage.set_realized_pnl(decision_id, …)` (fail-soft), and links the order to the decision (`orders.decision_id`). It is surfaced through `DecisionRecord.realized_pnl` and rendered per prior decision as `outcome: +X (win)` / `outcome: X (loss)` / `outcome: still open`. This is what actually delivers §4.1's fine-tuning loop.
   - **Done ✅ (Net-of-fee):** the PnL the LLM is shown is the *net* figure — `PaperExecutor` deducts per-side commission from cash and from `realized_pnl` (see §7.22), so the feedback loop reflects real economics rather than optimistic fee-free numbers. The risk engine's win/loss also keys off this net value (a fee-eaten-flat trade counts as a loss, matching the §4.3 "win rate > 50% after fees" gate). Covered by `TestFees` in `tests/unit/test_paper_executor.py` (229 tests passing).
   - **Caveat (review [R-M2/M3]):** complete *for paper mode*, but outcome attribution and fallback-row gaps make the loop half-blind — tracked as §7.8.

21. **Get the crypto agent running on real data** — ✅ **complete** (Week 4)
   - **Done ✅:** `scripts/run_crypto_agent.py` now always fetches **live public market data** via CCXT (`create_ccxt_provider(exchange_id=…, testnet=False)` — Kraken's public OHLCV endpoint needs no API key and no sandbox mode). Execution stays safe by default: no `KRAKEN_API_KEY` → `PaperExecutor` (fee + slippage aware, configured from `execution.paper_fee_pct` / `execution.paper_slippage_pct`); key set → `KrakenExecutor` on a **separate**, keyed, sandboxed client. `ImportError` from the lazy `ccxt` import fails fast with an actionable install hint. Covered by `TestBuildDataAndExecution` in `tests/unit/test_run_crypto_agent.py` (229 tests passing).
   - **Note:** the live smoke test could not be run in the development sandbox (outbound HTTPS to `api.kraken.com` blocked — environmental, not a code defect). Run `python -m scripts.run_crypto_agent` in a network-enabled environment to confirm the first cycle's logged decision. **Done ✅:**

22. **Add fee modeling to `PaperExecutor`** — ✅ **complete** (correctness, *not* just a verification)
   - **Done ✅:** `PaperExecutor.__init__` now takes `fee_pct` (default `0.0`). On a **buy** it deducts `cost + fee` from cash (and rejects if there isn't room for the fee); on a **sell** it credits `proceeds − sell_fee` and computes **net** `realized_pnl = gross − buy_fee − sell_fee`. The LLM-facing PnL and the §4.3 *"win rate > 50% after fees"* gate are now comparable to real economics.
   - **Config-driven:** `execution.paper_fee_pct` (0.26%/side, a typical crypto taker) and `execution.paper_slippage_pct` (0.1%) in `config/settings.yaml`, loaded via the new `ExecutionSettings` (old configs without the block default to a fee-free venue). Both entry scripts wire these into the paper executor.

23. **External review fixes (earlier round)** — ✅ verified; remaining deferred items now live as §7.5 (drawdown) and §7.8 (realized-PnL backfill)
   - **✅ Fixed — Timezone (market-hours guard was 1–2h off on a UTC host).** `StocksAgent.run_cycle` compared `datetime.now(UTC)` against a *Warsaw-local* window (e.g. `"09:00-16:30"`), so on a UTC host the guard ran the WSE window 1–2h off (CET/CEST). Fixed at the **call site** (not `is_market_open`, whose wall-clock contract is locked by `tests/unit/test_stocks_market_hours.py`): new `StocksAgent._local_now()` localizes `datetime.now(UTC)` to the market zone. The zone is **config-driven** — new `stocks_agent.market_timezone` (default `Europe/Warsaw` via `DEFAULT_MARKET_TIMEZONE`), wired through `AgentConfig` + `settings.yaml` + the runner; an unknown zone degrades gracefully to UTC. Covered by `TestLocalNowTimezone` (a UTC-host timestamp now maps into/out of the Warsaw window).
   - **✅ Fixed — SQLite concurrent writes (``database is locked`` under concurrent agents).** Added `PRAGMA journal_mode=WAL` in `Storage.initialize()` (`_enable_wal`), applied on the same sync engine used for `create_all`. WAL is a **persistent property of the SQLite file**, so it covers the async engine and every subsequent connection (dashboard/backtester readers). Covered by `test_initialize_enables_wal` (asserts `PRAGMA journal_mode` = `wal` through the async engine). This is P1's first sub-step of §7.15 and lands ahead of the dashboard.
