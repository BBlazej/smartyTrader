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
> **Built & tested (230 tests passing, ~95% coverage):** core (LLM client, risk engine, storage, scheduler, decision pipeline with inline indicators + prompt), crypto provider + executor + agent, **stocks provider (xAPI + yfinance) + executor + agent**, paper executor, monitoring (structured logging + Telegram alerts), both entry scripts, the **"learn from its own track record" loop** (the LLM now sees each prior decision **and its realized PnL outcome** — `realized_pnl` column + backfill on a closing order + rendered under `CONTEXT:`), **fee modeling in the paper executor** (`fee_pct` deducted from cash and from realized PnL, so paper PnL is net-of-fee), and the **crypto agent running on real data** (the paper path now fetches live public Kraken OHLCV via CCXT — no API key needed for public data — while execution stays simulated). All config-driven via `decision_history_limit` and the `execution:` block.
> **Not yet implemented (do not assume these exist):** news/sentiment feed, economic-calendar feed, `analysis/indicators.py` + `analysis/prompt_builder.py` (indicators & prompt currently live inline in `core/decision_pipeline.py`), `scripts/backtest.py`, XTB demo OAuth2 flow, dashboard, live-trading readiness items. See §7 Gaps & Next Steps.

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

## Phase 1 — Foundation (Weeks 1-2)

### 1.1 Project Scaffolding

```
trading_agent/
├── pyproject.toml
├── config/
│   ├── settings.yaml              # Global config (LLM, schedules, limits)
│   └── .env.example               # API keys, LLM endpoint
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
│       └── alerts.py              # Telegram/Discord notifications
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
4. Build prompt with portfolio state + market data ✅; **recent-decisions context — ✅ wired** (last N prior decisions injected under `CONTEXT:`, config `decision_history_limit`; **outcome/PnL — ✅ wired**, see §7.1)
5. Call LLM → structured signal ✅
6. Risk engine gate ✅
7. Execute on Kraken testnet or paper mode ✅
8. Store everything ✅

**Prompt design (target):** Include the last N decisions and their outcomes so the LLM can learn from its own track record. **Done ✅** — `build_user_prompt()` fetches the last N prior decisions via `get_recent_decisions()` and renders them under `CONTEXT:` (action, confidence, reasoning, risk verdict, timestamp), **and the *outcome* half is now wired** — each decision shows its net realized PnL (win/loss/still-open), net of fees, so the LLM learns not only what it decided but whether it paid off (see §7.1).

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
- Recent decisions and their outcomes (last 10)   ✅ actions/verdicts **and** realized-PnL outcome wired (see §7.1)
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
- Telegram bot for alerts on trades and risk rejections ✅ `monitoring/alerts.py`
- Simple web dashboard (Streamlit or Flask) showing: portfolio value over time, recent decisions, LLM confidence distribution, win rate — ⏳ *planned (Week 6)*

### 3.3 Backtesting (`scripts/backtest.py`) — ⏳ planned (Week 6)

Replay stored `market_snapshots` + `llm_decisions` against historical prices to calculate:
- Total return vs. buy-and-hold benchmark
- Win rate, average win/loss ratio
- Max drawdown, Sharpe ratio
- Per-symbol performance breakdown

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
  telegram_enabled: false
  telegram_bot_token: ""    # via .env (TELEGRAM_BOT_TOKEN)
  telegram_chat_id: ""      # via .env (TELEGRAM_CHAT_ID)
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
4. **Week 4:** Monitoring (structured logging + Telegram alerts) ✅; **decision-history prompt wiring** ✅; **crypto agent paper mode on real data** ✅ (the paper path now fetches live public Kraken OHLCV — no API key required — and executes via the fee/slippage-aware `PaperExecutor`; Kraken testnet execution remains opt-in via `KRAKEN_API_KEY`)
5. **Week 5:** `xtb_provider.py`, `xtb_executor.py`, `stocks_agent.py` + tests ✅ (68 new tests added)
6. **Week 6:** Backtesting framework, dashboard, alerting ◄ **(Next)**

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

Ordered by leverage. Items marked ⏳ are referenced as *planned/not yet implemented* throughout this plan.

1. **Close the "learn from its own track record" loop** — ✅ **complete** *(the plan's core differentiator)*
   - **Done ✅:** `build_user_prompt()` fetches the last N prior decisions via `get_recent_decisions()` and renders them under `CONTEXT:` (action, confidence, reasoning, risk verdict, timestamp); config-driven via `decision_history_limit` (default 10, 0 = off). The LLM now sees *what it decided and why it passed risk*.
   - **Done ✅:** the *outcome* half. `llm_decisions` now has a `realized_pnl` column (idempotent `ALTER TABLE` migration for pre-existing DBs). On a **closing** order (a fill that reports a non-`None` `realized_pnl`), the agent backfills it via `Storage.set_realized_pnl(decision_id, …)` (fail-soft), and links the order to the decision (`orders.decision_id`). It is surfaced through `DecisionRecord.realized_pnl` and rendered per prior decision as `outcome: +X (win)` / `outcome: X (loss)` / `outcome: still open`. This is what actually delivers §4.1's fine-tuning loop.
   - **Done ✅ (Net-of-fee):** the PnL the LLM is shown is the *net* figure — `PaperExecutor` deducts per-side commission from cash and from `realized_pnl` (see #6), so the feedback loop reflects real economics rather than optimistic fee-free numbers. The risk engine's win/loss also keys off this net value (a fee-eaten-flat trade counts as a loss, matching the §4.3 "win rate > 50% after fees" gate). Covered by `TestFees` in `tests/unit/test_paper_executor.py` (230 tests passing).

2. **Get the crypto agent running on real data** — ✅ **complete** (Week 4)
   - **Done ✅:** `scripts/run_crypto_agent.py` now always fetches **live public market data** via CCXT (`create_ccxt_provider(exchange_id=…, testnet=False)` — Kraken's public OHLCV endpoint needs no API key and no sandbox mode). Execution stays safe by default: no `KRAKEN_API_KEY` → `PaperExecutor` (fee + slippage aware, configured from `execution.paper_fee_pct` / `execution.paper_slippage_pct`); key set → `KrakenExecutor` on a **separate**, keyed, sandboxed client. `ImportError` from the lazy `ccxt` import fails fast with an actionable install hint. Covered by `TestBuildDataAndExecution` in `tests/unit/test_run_crypto_agent.py` (230 tests passing).
   - **Note:** the live smoke test could not be run in the development sandbox (outbound HTTPS to `api.kraken.com` blocked — environmental, not a code defect). Run `python -m scripts.run_crypto_agent` in a network-enabled environment to confirm the first cycle's logged decision.

3. **Backtesting (`scripts/backtest.py`)** ⏳ (Week 6)
   - Only meaningful after #1 and #2 have produced real stored snapshots/decisions.
   - Address LLM non-determinism (see Risks) so replay reproduces stored decisions.

4. **Data-enrichment feeds** ⏳ (optional, lower priority)
   - Sentiment provider (crypto) and economic-calendar feed (stocks) are still aspirational. Ship #1–#3 first; add these as the prompt benefits from richer context.

5. **Split `analysis/` out of `core/decision_pipeline.py`** (housekeeping)
   - Indicators (`compute_indicators`, `_compute_rsi`, `_compute_macd`, `_compute_bollinger_bands`) and the default system prompt currently live in `core/decision_pipeline.py`. Move to `analysis/indicators.py` + `analysis/prompt_builder.py` for the plan's intended layout *once* the prompt work in #1 lands, to avoid churning the module twice.

6. **Add fee modeling to `PaperExecutor`** — ✅ **complete** (correctness, *not* just a verification)
   - **Done ✅:** `PaperExecutor.__init__` now takes `fee_pct` (default `0.0`). On a **buy** it deducts `cost + fee` from cash (and rejects if there isn't room for the fee); on a **sell** it credits `proceeds − sell_fee` and computes **net** `realized_pnl = gross − buy_fee − sell_fee`. The LLM-facing PnL and the §4.3 *"win rate > 50% after fees"* gate are now comparable to real economics.
   - **Config-driven:** `execution.paper_fee_pct` (0.26%/side, a typical crypto taker) and `execution.paper_slippage_pct` (0.1%) in `config/settings.yaml`, loaded via the new `ExecutionSettings` (old configs without the block default to a fee-free venue). Both entry scripts wire these into the paper executor.

7. **XTB demo OAuth2 flow** ⏳
   - `run_stocks_agent.py` currently falls back to the paper executor until the OAuth2 flow lands. Required before real XTB demo trading; the stocks *agent* and *provider* logic is already built and tested against the paper executor.
