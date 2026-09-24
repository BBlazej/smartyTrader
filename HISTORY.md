# Project History & Delivered Work

The delivery record of the autonomous trading agent project: what has been built, when, and how it was verified. Extracted from `PLAN.md` on **2026-09-17**, when PLAN.md became a gaps/todos-only document (see [PLAN.md](PLAN.md)); architecture content lives in [ARCHITECTURE.md](ARCHITECTURE.md).

**Reading conventions**

- **§7.N identifiers are never renumbered.** Completed items from PLAN.md §7 appear below under their original numbers; cross-references from code comments, `AGENTS.md` and `README.md` keep resolving.
- "Phase N" / "Week N" headings reproduce the original planning timeline. Phase 3 (risk/monitoring) design now lives in [ARCHITECTURE.md](ARCHITECTURE.md); Phase 4 (iteration/live-readiness) is still open and stays in [PLAN.md](PLAN.md).
- `[R-xx]` severity tags reference `review.MD` / `review2.md` at the repo root.

## Status snapshot

> ### Status (as of this revision)
> **Built & tested (465 tests passing, ~93% coverage):** core (LLM client, risk engine, storage, scheduler, decision pipeline, with its indicators & prompt now extracted into the `analysis/` layer (§7.17)), crypto provider + executor + agent, **stocks provider (xAPI + yfinance) + executor + agent**, paper executor, monitoring (structured logging), both entry scripts, the **"learn from its own track record" loop** (the LLM sees each prior decision **and its realized PnL outcome** — now attributed FIFO to the *entry* decision through the shared `execution/position_tracker.py`, on paper and on real venues alike — §7.8), **fee modeling in the paper executor**, the **crypto agent running on real data** (paper path fetches live public Kraken OHLCV via CCXT; execution stays simulated), the **timezone-aware market-hours guard**, **SQLite WAL mode**, **weekend/holiday-aware market-hours guard with wrap-around windows** (§7.10), **per-cycle position marking** (§7.1), **all seven risk rules live** including the peak-equity drawdown gate and the notional cap enforced at the gate (§7.5), a **hardened keyed-Kraken path** (real ccxt balance/fill payloads; spot `fetch_positions` degradation — §7.6, live testnet smoke still pending), **restart-safe paper portfolio + risk state** rehydrated from SQLite (`core/rehydration.py`, §7.7), **storage retention pruning** (startup + scheduled passes, `orders.created_at` for un-filled rows; portfolio snapshots exempt — §7.12), a **shared base agent + runner factory** (`agents/base_agent.py`, `core/runner.py` — the two agents and two scripts are now thin market-specific shells — §7.13), and **deterministic stop-loss / take-profit enforcement** (levels carried on positions; a breach closes the position on the next cycle without an LLM call and bypassing the gate — §7.9), and the **standalone web dashboard** (`src/dashboard/` + `scripts/run_dashboard.py`: FastAPI + Jinja2/HTMX monitor — uPlot portfolio chart, positions, decisions with win-rate/confidence stats, health cards — plus HTMX pause/resume/close-all controls and a safe-config editor, all writing the `agent_control` latches directly so they work with or without the agent-side control API — §7.15 P3/P4), and **Docker/compose packaging** (one slim image; `docker-compose.yml`: both agents + dashboard + on-demand backtester on the shared `agent-data` volume — §7.15 P5), and **real XTB demo execution over xAPI** (`execution/xtb_client.py`: WebSocket login with the xStation verification code, instant orders + fill-status polling, live position marks via `getTickPrices`, reconnect-once; opt-in via `xtb_execution.enabled` + env credentials — paper stays default — §7.16), and the **`analysis/` layer extraction** (`analysis/indicators.py` + `analysis/prompt_builder.py`: all indicator math and prompt construction moved verbatim out of `core/decision_pipeline.py`, which now only orchestrates — §7.17), and the **code-nits bundle** (public `get_portfolio_state` / `cooldown_until` accessors replacing private pokes, an honest `DailyLossTracker._latest_value`, config-driven `risk.consecutive_losses_threshold` + `llm.temperature`/`max_tokens`/`retry_backoff_base_seconds` with real exponential retry backoff, robust chat-URL resolution replacing the `/v1` string surgery, structlog in the risk engine, `PipelineStep` a real `StrEnum`, Wilder-vs-simple-average indicator notes — §7.19). Config-driven via `decision_history_limit`, the `execution:` block, `stocks_agent.market_timezone` / `market_holidays`, `risk.enforce_exit_levels`, and the `dashboard:` block (host/port/refresh/agents).
> **Not yet implemented (do not assume these exist):** news/sentiment feed, economic-calendar feed, live execution against anything but paper/Kraken-testnet/XTB-demo opt-in paths (the dashboard, control plane and Docker packaging — §7.15 in full — and XTB demo xAPI execution — §7.16 — have since landed; see ARCHITECTURE.md), and **venue-side** stop/take orders (our SL/TP checks are local to the agent). Full list incl. findings from the 2026-09-15 code review (`review.MD`): see §7 Gaps & Next Steps.

## Delivered milestones (original implementation order)

1. **Week 1:** `pyproject.toml`, config, `llm_client.py`, `storage.py` + tests ✅
2. **Week 2:** `risk_engine.py`, `decision_pipeline.py`, `paper_executor.py` + tests ✅
3. **Week 3:** `ccxt_provider.py`, `kraken_executor.py`, `crypto_agent.py`, `scheduler.py`, `run_crypto_agent.py` + integration tests ✅
4. **Week 4:** Monitoring (structured logging) ✅; **decision-history prompt wiring** ✅; **crypto agent paper mode on real data** ✅ (the paper path now fetches live public Kraken OHLCV — no API key required — and executes via the fee/slippage-aware `PaperExecutor`; Kraken testnet execution remains opt-in via `KRAKEN_API_KEY`)
5. **Week 5:** `xtb_provider.py`, `xtb_executor.py`, `stocks_agent.py` + tests ✅ (68 new tests added)
6. **Week 6:** **decision-replay backtesting** on fresh historical candles ✅ (§7.14); Docker dashboard packaging — landed later under §7.15 P3–P5 (milestones 8–9 below)
7. **Week 6 (cont.):** **control plane P1/P2** — `agent_control` table + per-cycle agent checks (overrides → close-all → pause, heartbeat) + agent-side FastAPI control API with the safe-config whitelist ✅ (§7.15; dashboard pages + Docker packaging followed — milestones 8–9 below). Between the weeks above, every review finding landed in order: §7.1–§7.14 and §7.20–§7.23 (detailed write-ups below).
8. **Week 6 (cont.):** **dashboard P3/P4** — standalone FastAPI + Jinja2/HTMX web dashboard (`src/dashboard/app.py::create_dashboard_app`, pure view-models in `views.py`, `scripts/run_dashboard.py`): overview page with uPlot portfolio-value chart (`/api/portfolio.json` polling), positions, decisions + win-rate/avg-confidence/confidence-histogram stats, HTMX-polled health cards; Pause/Resume/Close-all buttons and a safe-config form writing the same `agent_control` latches as the control API (config validated via `validate_overrides_payload` → `SafeConfigOverrides`, credential-shaped keys rejected wholesale). Reads the shared SQLite DB (WAL) as a reader — no HTTP coupling to the agent process. 22 new tests (§7.15 P3/P4 ✅).
9. **Week 6 (cont.):** **Docker packaging P5** — `Dockerfile` (python:3.11-slim, non-root) + `docker-compose.yml`: `agent-crypto`, `agent-stocks`, `dashboard` (loopback-only port mapping, `/healthz` healthcheck) and on-demand `backtester` (`tools` profile); shared `agent-data` named volume for the SQLite WAL, `./config` bind-mounted read-only (safe overrides live in the DB, so nothing writes config files), secrets via environment substitution only (`.env` docker-ignored). Packaging made deterministic in `pyproject.toml` (explicit setuptools discovery + templates as package-data) and shipped `scripts/__init__.py`. Image build + containerized dashboard/agent-cycle smoke verified (§7.15 P5 ✅ — **§7.15 complete**).
10. **Week 6 (cont.):** **XTB demo execution via xAPI** — real WebSocket client `execution/xtb_client.py` behind the existing executor seam (login auth — *not* OAuth2, which doesn't exist; hosts moved to `wss://ws.xapi.pro` in 2025), instant orders + status polling, tick-marked positions, reconnect-once; opt-in wiring in the stocks runner (`xtb_execution` config + env credentials, paper stays default and outside the dashboard whitelist). 20 new tests (§7.16 ✅).
11. **Week 6 (cont.):** **`analysis/` layer extraction** — indicator math (`compute_indicators` + RSI/MACD/Bollinger/ATR helpers) and prompt construction (`build_user_prompt`, `DEFAULT_SYSTEM_PROMPT`) moved verbatim from `core/decision_pipeline.py` into `analysis/indicators.py` + `analysis/prompt_builder.py`; the pipeline now only orchestrates. Zero behavior change; same 456 tests (§7.17 ✅).
12. **Week 6 (cont.):** **code-nits bundle** — encapsulation cleanup (`get_portfolio_state`, `cooldown_until`), honest optional state in `DailyLossTracker`, config-driven consecutive-loss threshold + LLM sampling knobs, exponential retry backoff + robust chat-URL resolution in the LLM client, structlog in `risk_engine.py`, `PipelineStep` → `StrEnum`, indicator-methodology docstrings (9 new tests, §7.19 ✅).

## Phase 1 — Foundation (Weeks 1-2) — original plan & delivery

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
│   ├── agents/                    # Per-market agents (thin subclasses, §7.13)
│   │   ├── base_agent.py          # Shared cycle loop + post-process + persistence
│   │   ├── crypto_agent.py        # Kraken agent
│   │   └── stocks_agent.py        # XTB agent (+ market-hours guard)
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
│   └── backtest.py                # Decision replay over stored decisions vs fresh candles (§7.14)
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

## Phase 2 — Agents (Weeks 3-4) — original plan & delivery

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

## Completed §7 items — A. Low-hanging fruit

Moved verbatim from PLAN.md §7.A on 2026-09-17. Original item numbers (=§7.N) and severity tags preserved; each was committed with tests at the test count stated in its write-up.

### §7.1 — Refresh paper-position prices every cycle — ✅ complete [R-H1]

   - **Done ✅:** `DecisionPipeline.run` now calls the new `_mark_positions(symbol, snapshot)` immediately after the fetch step and **before** the risk check: it feeds the snapshot's last close into the optional executor hook `update_price(symbol, close)`, which `PaperExecutor` implements (no-op for symbols with no open position). Real-venue executors (Kraken/XTB) report live prices and don't implement the hook, so they're skipped via duck-typing; marking is fail-soft (a failure logs a warning, never breaks the cycle). Result: `unrealized_pnl` moves with the market, `_get_portfolio_state()` (risk gate + the agent's `update_daily_value` baseline) sees total_value at market — making the `-daily_loss_limit_pct: 0.02` guard honest in paper mode — and persisted portfolio snapshots carry current marks. Covered by `TestPositionMarking` in `tests/unit/test_decision_pipeline.py` (re-mark to last close with `unrealized_pnl != 0`; risk check receives the market-valued portfolio; no phantom positions; hook-less executors unaffected). 233 tests passing.
   - **Side effect found while testing:** sell sizing (`max_position_pct × total_value`) is not clamped to units held, so a marked-to-market total can now produce a sell slice larger than the position → rejected order. Pre-existing behavior (tracked in §7.19's float/sizing nits), surfaced not caused by this fix.

### §7.2 — Fix `enabled: false` semantics — ✅ complete [R-H3]

   - **Was:** both runners treated "disabled" as *run one full cycle — including order placement, LLM calls and DB writes — then exit*. A user who disabled an agent to be safe still got trades executed.
   - **Done ✅:** both `scripts/run_crypto_agent.py` and `scripts/run_stocks_agent.py` now check `<agent>.enabled` immediately after loading settings/logging and **exit before constructing any component** — no storage init, LLM client, provider/executor, pipeline or agent, so no cycles, orders or DB writes are possible. Single-cycle mode is now an explicit CLI flag: `python -m scripts.run_crypto_agent --once` (same for stocks) runs exactly one full cycle with a guaranteed clean shutdown (`agent.shutdown` + provider/executor/storage closes via `try/finally`; a failing cycle propagates so cron sees a non-zero exit) and never starts the scheduler. Verified live: running the disabled stocks agent logs the warning and exits 0 without touching the DB.
   - **Tests:** new `TestEnabledSemantics` in `tests/unit/test_run_crypto_agent.py` (disabled → storage/LLM/pipeline/agent factories never called; `--once` → exactly one cycle + closes + scheduler not started; failed `--once` cycle still releases resources) and a new mirror file `tests/unit/test_run_stocks_agent.py` (the stocks runner previously had no tests at all). 239 tests passing.

### §7.3 — Dependency & test-claims cleanup — ✅ complete [R-M4]

   - **Was:** `pandas-ta` and `python-dotenv` declared in `pyproject.toml` but never imported (indicators are hand-rolled; `.env` loading is a dependency-free `_load_dotenv()`); no dev-dependency group; `AGENTS.md` claimed `hypothesis` + `responses` property tests that didn't exist; the stocks runner logged fetch errors every cycle when `yfinance` was missing.
   - **Done ✅:** removed `pandas-ta` and `python-dotenv` from dependencies (verified nothing imports them); added `[project.optional-dependencies].dev` (`pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `ruff`) so `pip install -e ".[dev]"` reproduces the test env; installed `hypothesis`; added real property-based tests for risk-engine invariants in `tests/unit/test_risk_engine_properties.py` (HOLD always approved; approved active signals carry a stop; sub-minimum confidence rejected; opening beyond `max_open_positions` rejected; breached daily-loss blocks active trades; 3 consecutive losses ⇒ cooldown; deterministic verdicts). `AGENTS.md` corrected — `responses` dropped from the claim (it mocks `requests`, unused here), dev group documented.
   - **Also:** `create_xtb_provider` now checks for `yfinance` eagerly and both it + the stocks runner fail fast with an actionable install hint (mirrors the ccxt one) instead of erroring every cycle; new `TestYFinanceFailFast` covers it. 247 tests passing.

### §7.4 — Docs/reality mismatches — ✅ complete [R-L]

   - **Done ✅:** `.env.example` — one now lives at the **repo root** (where both runners load `.env` from), with all keys commented out/blank and the XTB + JSON-schema opt-in entries merged in; the divergent duplicate `config/.env.example` was deleted. (`AGENTS.md` docs path corrected `doc/` → `docs/`.)
   - **Done ✅:** README no longer claims "7 deterministic risk rules" while two are stubs — the layout comment now reads *5 live rules; drawdown + notional cap land in §7.5* (restored to 7 once §7.5 lands). Test counts refreshed.

## Completed §7 items — B. High severity

### §7.5 — Close the two no-op risk rules (drawdown; position size at the gate) — ✅ complete [R-H2]

   - **Done ✅ (drawdown, option b):** `RiskEngine._check_drawdown` is live: it tracks a high-water mark (`note_equity`, lazily seeded from the first reading) and rejects any active signal while equity is more than `risk.max_drawdown_pct` below the peak. Cross-restart persistence uses **SQLite**: `Storage.get_max_portfolio_value()` (MAX over `portfolio_snapshots.total_value`) seeds the engine at startup in both runners (`seed_peak_equity`, fail-soft), so a restart can't reset the guard. The engine stays a pure sync object — the peak is fed in from storage by the caller, mirroring `update_daily_value`.
   - **Done ✅ (size at the gate):** the pipeline now computes the planned quantity *before* the risk check and passes `planned_notional = quantity × price` into `evaluate()`; `_check_position_size` rejects when it exceeds `max_position_pct × total_value` (tiny float tolerance), and the approved plan is reused unchanged at execution — a sizing regression can no longer slip past approval. Sell sizing is additionally clamped to units actually held, which kills the oversized-sell → guaranteed-rejection loop surfaced as §7.1's side effect.
   - **Tests:** `TestDrawdown` + `TestPositionSizeGate` (`test_risk_engine.py`), `get_max_portfolio_value` pair (`test_storage.py`), `TestSizingAtTheGate` — gate receives the notional, an oversized plan is rejected end-to-end with a real engine, sells clamp to held units (`test_decision_pipeline.py`). 260 tests passing.
   - **Was:** `_check_drawdown` returned `APPROVED` unconditionally (config value unused), and `_check_position_size` only guarded total value ≤ 0 — actual sizing happened after approval with nothing validating it.

### §7.6 — Real-CCXT integration pass for the keyed Kraken path — ✅ code pass complete; live-testnet smoke still pending [R-H4]

   - **Done ✅:** `get_cash` now parses real ccxt `fetch_free_balance` payloads (currency→`{free,used,total}` dicts, case-insensitive quote lookup, missing quote → 0.0, bare-float stubs still work) instead of `float(balance)` on a dict; closed orders record the **fill average price** and `filled_at` from ccxt's ms timestamps (`updated`/`closedAt`/`timestamp`), with filled-quantity preference over requested; `get_positions` survives Kraken-spot's `fetch_positions` rejection (warns once, returns `[]`) instead of raising every keyed cycle. Documented the remaining venue limitations in the module docstring. Tests: `TestRealCcxtShapes` uses payloads shaped like recorded ccxt 4.5.x responses (266 tests passing).
   - **Still open:** a live keyed smoke run against the Kraken testnet from a network-enabled environment (sandbox blocks outbound HTTPS — see `nightly_finds.md` #1), and per-cycle reconciliation of orders left `open` (marketable limits at last close usually return `closed` in `create_order`, so this is now rare but unpinned).

### §7.7 — Persist / rehydrate paper portfolio + risk state across restarts — ✅ complete [R-M1]

   - **Done ✅:** new `src/core/rehydration.py` with one fail-soft startup call (`rehydrate_from_storage`) wired into both runners before any cycle: the paper book is restored from the latest `portfolio_snapshots` row via a new `PaperExecutor.load_portfolio_state(cash, positions)` hook (live-venue executors lack the hook → skipped); the daily-loss baseline is rehydrated from today's *earliest* snapshot (`get_first_portfolio_snapshot_of_day`); and the losing streak + still-running cooldown are rebuilt from trailing negative outcomes in closed decisions (`get_closed_decisions`, newest-first walk; cooldown restarts from the newest loss's timestamp + `consecutive_losses_cooldown_minutes`).
   - **Done ✅:** `initial_cash` is config-driven — new `execution.initial_cash` (default 100 000) seeds a *fresh* paper portfolio only; after the first cycle the persisted snapshot wins. Both runners pass it to `PaperExecutor`.
   - **Tests:** `tests/unit/test_rehydration.py` (cash/positions/marks restored, no-snapshot and hook-less skips, baseline honesty after restart, streak+cooldown restored, recent win breaks streak, combined entry point) plus new storage query tests. 276 tests passing.

### §7.25 — Rehydrate FIFO PositionTracker ledgers across restarts — ✅ complete [R2-1.1, R3-H2, find #7]

   - **Done ✅:** `rehydrate_paper_executor` now queries the full chronological filled-order history (`Storage.get_filled_orders()` — `status == "filled"`, id-ascending, optional symbol filter) and feeds it into `PaperExecutor.load_portfolio_state(cash, positions, fills=...)` as new `FillRecord`s (`execution/position_tracker.py`). Buys re-open lots **with their originating `decision_id`**, historical sells consume them FIFO — so a position opened before a restart still attributes closing-sell PnL back to its entry decision (`closed_entries`) instead of reporting a decision-less outcome. Stored orders carry no commission, so rebuilt lots are fee-free (documented); any quantity gap between replayed fills and the loaded book (e.g. order history pruned by §7.12 retention) is topped up with one synthetic lot per position at its `avg_entry_price`, keeping tracker and book consistent — exactly the pre-§7.25 fallback.
   - **Tests:** `TestFillLedgerRehydration` in `test_rehydration.py` (two lots + partial sell before restart → post-restart tail-sell realizes FIFO basis of lot 2 with `{entry_decision_id: 2}` attribution; pruned-history fallback to synthetic lots) and a chronological/status-filter test for `get_filled_orders` in `test_storage.py`. 500 tests passing.

### §7.26 — Config-driven loss-streak threshold in state rehydration — ✅ complete [R3-H1, find #15]

   - **Done ✅:** `rehydrate_risk_engine` no longer hardcodes `streak >= 3` when deciding whether a restarted losing streak should re-arm the cooldown; it reads `risk_engine.settings.consecutive_losses_threshold` (same config knob the live tracker uses since §7.19), so restart-time and in-process cooldown policy can never diverge.
   - **Tests:** `test_cooldown_uses_configured_threshold_not_three` (streak of 3 with threshold 5 survives restart *without* cooldown) and `test_cooldown_restored_at_custom_threshold` (threshold 2 re-arms it) in `tests/unit/test_rehydration.py`. 497 tests passing.

## Completed §7 items — C. Medium severity (§7.8–§7.16, §7.27, §7.29–§7.33)

### §7.8 — Decision-history quality: attribute outcomes to entry decisions; exclude fallback rows — ✅ complete [R-M2/M3] *(absorbs the earlier external-review item "Deferred #4")*

   - **Done ✅ (shared FIFO tracker):** new `src/execution/position_tracker.py` (`PositionTracker` + `_Lot`/`SellOutcome`) is fed by **all three executors**, so closing fills now report `OrderResult.realized_pnl` everywhere — Kraken and XTB previously left it `None`, which silently no-oped the whole "learn from your track record" backfill on real venues. Lots carry the entry `decision_id`, and a sell returns `closed_entries: list[ClosedEntry]` attributing net PnL per originating decision (buy-side commission pro-rated per lot, sell-side fee pro-rated across the fill). Paper keeps exact fee honesty (single-lot FIFO reproduces the old average-cost numbers); venue fills are **gross of commission** because `create_order` payloads report none — documented in both executors. A venue sell with nothing locally tracked (holdings opened before a restart) reports *no* outcome rather than a fabricated break-even one.
   - **Done ✅ (outcomes reach the entry row):** `Executor.place_order` gained a `decision_id` kwarg and the pipeline now persists each decision **itself, immediately after the risk gate** (`DecisionPipeline._persist_decision`, fail-soft; agents lost their duplicate `_persist_decision`), so the id exists *before* the fill. Agents backfill via the new `Storage.add_realized_pnl(decision_id, delta)` (COALESCE-sum, so multi-tranche closes accumulate) over `order_result.closed_entries`, while the sell row keeps its own `set_realized_pnl` stamp.
   - **Done ✅ (fallback rows quarantined):** `TradeSignal.is_fallback` (never forgeable — `_parse_signal` strips it from model output) → new `llm_decisions.is_fallback` column (added by the existing ALTER-TABLE migration), set by the LLM client's exhausted-retries HOLD; `get_recent_decisions` filters them out, so they stay in the DB for audit but never re-enter prompt context.
   - **Done ✅ (audit logging, §3.3):** `llm_client.py` moved to structlog and logs a full `llm_exchange` event (system prompt + user prompt + raw response) per live decision; retries log `llm_attempt_failed` / `llm_retries_exhausted`.
   - **Tests:** new `tests/unit/test_position_tracker.py` (FIFO order, per-entry aggregation, pro-rated fees both sides, partial consumption, dust pop); attribution classes in `test_paper_executor.py` (incl. rehydrated-book cost basis), `test_kraken_executor.py`, `test_xtb_executor.py` (untracked/pending/unpriced sells report nothing); `test_storage.py::TestDecisionAttribution`; fallback-flag tests in `test_llm_client.py`; `TestDecisionPersistence` in `test_decision_pipeline.py`. Both integration suites now drive a **real** `DecisionPipeline` (only provider + LLM mocked) and pin the two-cycle buy→sell story: order→decision links, sell-row stamping, entry-row backfill. **309 tests passing, 94% coverage.**
   - **Real-venue realized PnL never backfilled (feedback loop skipped for Kraken/XTB):** `PaperExecutor` computes net `realized_pnl` on sells (average-cost), but `KrakenExecutor` / `XTBExecutor` never set `realized_pnl`, and both agents gate the backfill on `order_result.realized_pnl is not None` — so the "learn from its track record" loop silently no-ops on real venues. **Decision needed before building:** a shared cost-basis **`PositionTracker`** (FIFO vs LIFO — recommend **FIFO**) used by **all** executors so `OrderResult.realized_pnl` is populated on closing fills, unifying paper (currently average-cost) and real venues. It must actually set `OrderResult.realized_pnl` on closing fills or the agent gate still won't fire; then add executor tests asserting `realized_pnl` on a closing sell.
   - **Outcomes land on the wrong row:** the realized PnL of a closing sell is stamped onto *the sell's own decision row*, while the originating buy row stays `outcome: still open` forever — the LLM never sees how its entry decisions turned out. Use `orders.decision_id` + the `PositionTracker` above to backfill the opening decision when its position closes.
   - **LLM-fallback rows pollute context:** after exhausted retries, the HOLD fallback (reasoning "LLM unavailable…") is persisted as a normal *approved* decision and re-fed into subsequent prompts as one of the last-N. Mark fallback decisions (dedicated column or verdict value) and exclude them from `get_recent_decisions`. Also close PLAN's own audit requirement (§3.3 note): log full prompt+response for live decisions — today only `signal.action` is logged.

### §7.9 — Deterministic stop-loss / take-profit enforcement — ✅ complete [R-M5]

   - **Done ✅:** `Position` now carries optional `stop_loss`/`take_profit`, attached at entry via new `Executor.place_order(stop_loss=…, take_profit=…)` kwargs (paper stores them on its book; Kraken/XTB keep a local `_exit_levels` map and re-attach it to the positions they report, dropping entries when the position closes). Because levels live on positions — which portfolio snapshots persist — they survive restarts with no new storage work.
   - **Done ✅:** `DecisionPipeline._check_exit_levels` runs every cycle right after marking (step 1c, before indicators/LLM): if the mark is ≤ stop or ≥ take-profit, it closes the *full* held quantity through the executor and ends the cycle with `PipelineResult.auto_exit=True` + `exit_reason` (`stop_loss`/`take_profit`). **The LLM is never consulted and the risk gate is deliberately bypassed** — exits only reduce exposure, so a cooldown or daily-loss block must never strand a position (previously true: see `nightly_finds.md` #6). The realized outcome still feeds the loss-streak tracker, and §7.8's `closed_entries` backfill lands the PnL on the entry decision; agents raise an `exit_level` alert.
   - **Done ✅ (unblocks itself):** the stop-loss gate no longer demands a stop on *closes* — `_check_stop_loss` requires one for entries only (`"Opening a position requires a stop-loss"`). Requiring an exit plan on an exit added nothing and blocked legitimate sells.
   - **Config-driven:** `risk.enforce_exit_levels: true` (default on) turns the whole guard off; levels are *local* checks, not venue-side stop orders (venue-side OCO remains future work).
   - **Tests:** `TestExitLevelEnforcement` (stop hit closes without an LLM call; take-profit books profit; no breach → normal path; enforcement disableable; exit fires while the engine is in cooldown), `TestExitLevels` (paper: attach/omit/update-on-add, survive rehydration), `TestExitLevelCarrying` for both venue executors (attach → drop on close), risk-engine test + property for the entry-only stop rule, and an end-to-end `TestAutoExit` in the crypto integration suite (exit order persisted with no decision link, entry row backfilled, no second decision row). **323 tests passing, 94% coverage.**

### §7.10 — Weekend/holiday awareness in the stocks market-hours guard — ✅ complete [R-M6]

   - **Done ✅:** the guard moved from time-of-day-only to `market_closed_reason(now, market_hours, holidays)` (with `is_market_open` kept as a thin wrapper): Saturdays/Sundays are always closed when a real window is configured (a no-op spec like `"24h"` still disables the whole guard), and any date in the new config-driven `stocks_agent.market_holidays` list (ISO `YYYY-MM-DD`, validated eagerly at agent construction — a typo aborts startup instead of silently disabling the holiday check) closes the cycle. The overnight-window trap is fixed too: `start > end` (e.g. `"22:00-08:00"`) now wraps across midnight (`t >= start or t <= end`) instead of producing the never-true comparison that silently kept the agent from ever running. `run_cycle` logs the skip *reason* (`weekend` / `holiday` / `outside trading window`).
   - **Tests:** `TestWeekendGuard`, `TestOvernightWindow`, `TestHolidays` (incl. loud failure on malformed dates) and `TestAgentCycleSkip` (weekend + holiday cycles skipped without touching the pipeline; malformed holiday config raises at construction). 337 tests passing.
   - **Deliberate limit:** weekend closure takes precedence over a wrapped window (a Fri-night→Sat-morning window is closed after midnight) — correct for the WSE, revisit if a 24h-adjacent venue is configured. A real holiday *calendar* feed remains future work; the config list covers known closures.

### §7.11 — Stocks data depth silently disables indicators — ✅ complete [R-M7]

   - **Done ✅:** `YFinanceSource._PERIOD_MAP["1d"]` now requests `"6mo"` of daily bars (≈ 126 trading days ≥ MACD's 26-close minimum + signal window, and enough for the 100-candle snapshot cap) instead of `"1mo"` ≈ 21 closes, so the stocks prompt carries MACD like crypto does.
   - **Done ✅ (NaN handling):** `_f()` no longer zero-fills — NaN OHLC cells are detected in `_to_rows` and the whole row is **dropped** (with a counted warning log), instead of becoming a zero-low candle that poisons ATR/Bollinger downstream. Volume stays non-critical: NaN volume → 0.0 via `_nan_to_zero`.
   - **Tests:** `TestPeriodMap` pinned to `"6mo"` + `test_daily_period_deep_enough_for_macd`; `TestNaNHandling` (missing-Low/High row dropped with no fabricated zeros, NaN volume → 0.0, limit applied after dropping). Live yfinance validation still impossible from the sandbox (`nightly_finds.md` #2) — covered through the injected-source seam + fake frames. 341 tests passing.

### §7.12 — Storage retention / pruning — ✅ complete [R-M8]

   - **Done ✅:** `Storage.prune(snapshot_days, history_days)` deletes expired rows and returns per-table counts. Config under `storage:`: `snapshot_retention_days` (default **30** — market snapshots are the space hog at ~100-candle JSON per symbol-cycle, and pure re-creatable cache since the backtester pulls fresh candles), `history_retention_days` (default **0 = keep forever** — decisions/orders are the trade record: audit trail, fine-tuning dataset, cooldown rehydration), `prune_interval_minutes` (default 1440).
   - **Done ✅:** both runners prune once at startup (fail-soft via `core/retention.py::prune_storage`, so even `--once` cron usage stays hygienic) and schedule a recurring `storage_prune` job while running. New `scripts/prune_storage.py` runs the same policy out-of-band for an already-bloated DB without starting any agent.
   - **Schema:** `orders.created_at` (nullable → migration adds it to pre-existing DBs and backfills from `filled_at`; new saves stamp it) — retention needs a storage-time bound for orders that never filled, which `filled_at` alone cannot provide.
   - **Deliberate:** `portfolio_snapshots` are **never** pruned — the drawdown high-water seed (`get_max_portfolio_value`) reads MAX over their full history, and pruning them would silently weaken that guard after a restart; they're also tiny (no candle blobs).
   - **Tests:** `TestPruning` (old snapshots go / recent stay; history window off keeps decisions+orders; on ⇒ both pruned with counts; `created_at` stamped on save; portfolio snapshots survive aggressive pruning; disabled windows = no-op), `TestMigrations` (legacy DB without `orders.created_at` gets the column + backfill), `test_retention.py` (wrapper passes config through, skips when disabled, fail-soft on DB errors), and startup-prune wiring tests in both runner suites. 353 tests passing.

### §7.13 — Deduplicate agents + runners — ✅ complete [R-M9]

   - **Done ✅ (base agent):** new `src/agents/base_agent.py::BaseTradingAgent` holds the whole shared lifecycle — cycle loop, `_post_process` (daily-value update, order persistence, §7.8 realized-PnL backfill), portfolio snapshot, alerts, start/stop/shutdown. Market-specific behavior hooks in: `_skip_cycle_reason()` (stocks' weekend/holiday/window guard overrides it; crypto inherits "never skip") and `_start_log_fields()`. `CryptoAgent` / `StocksAgent` are now thin subclasses with identical public constructors.
   - **Done ✅ (runner factory):** new `src/core/runner.py` — one implementation of `load_dotenv`, `build_alerts`, the enabled-gate (still exits *before constructing anything*), storage/LLM/risk wiring, drawdown seeding, rehydration + retention pruning, pipeline build, the `--once` path and the scheduled loop with guaranteed cleanup. Both scripts keep only their market-specific seams (`_build_data_and_execution` for crypto's keyed-testnet switch, `_make_components` for stocks' yfinance fail-fast + XTB notice) passed as callbacks.
   - **Tests:** runner tests retargeted to `src.core.runner.*` patch points (script-level factories still asserted where market-specific); new `TestScheduledModeLifecycle` pins the previously-uncovered shared scheduled loop: agent started, first cycle immediate, both jobs (`crypto_cycle` + `storage_prune`) scheduled, cancel ⇒ full cleanup. Net **−500 lines**; 94% coverage held. 354 tests passing.

### §7.14 — Backtesting (`scripts/backtest.py`) — ✅ complete (Week 6)

   - **Done ✅ (type):** decision replay as locked — `src/core/backtester.py::DecisionReplayBacktester` re-simulates stored `llm_decisions` against fresh historical candles through the **same** `RiskEngine` and `PaperExecutor` fee/slippage model as live. Deterministic, zero LLM calls (identical inputs ⇒ identical report, pinned by test).
   - **Done ✅ (shared rules):** extracted `exit_level_breach()` + `calculate_quantity()` out of `DecisionPipeline` into module-level functions in `core/decision_pipeline.py` — the live pipeline delegates to them, so replay sizing and §7.9 exit enforcement can never drift from live.
   - **Done ✅ (history ingestion):** `CCXTProvider.fetch_history` paginates CCXT `fetch_ohlcv(since=…)` pages up to a page cap (dedupes by timestamp, stops on no-progress or ≥ end); `YFinanceSource.fetch_history` + `XTBProvider.fetch_history` do the same for stocks (yfinance's exclusive `end` padded, rows strictly filtered to the window; sources without range support raise `TypeError`). `Storage.get_decisions_in_range(start, end, symbols, include_fallback)` feeds the replay (fallback rows excluded by default, naive-UTC comparisons).
   - **Replay semantics:** merged candle/decision timeline (candle-before-decision at equal timestamps mirrors a live cycle); every candle re-marks open positions (`update_price`), exit levels auto-close on breach *without* the risk gate (same as §7.9), entries are sized by `calculate_quantity` and gated by `RiskEngine.evaluate(planned_notional=…)` — what the gate approves is what fills; realized outcomes feed the loss-streak/cooldown tracker exactly like live.
   - **Done ✅ (metrics/report):** total return vs per-symbol buy-and-hold (+ equal-weight blend), max drawdown, annualized Sharpe (coarse for stock gaps — documented), win rate, avg win/loss, auto-exit / risk-rejected / hold counts, per-symbol breakdown, equity curve. `scripts/backtest.py` CLI: `--start/--end/--days/--symbols/--provider ccxt|yfinance/--timeframe/--report out.json`; prints a human summary, optional full JSON.
   - **Known simplification (documented in module docstring + `nightly_finds.md` #10):** the risk engine's daily-loss/cooldown trackers key off wall-clock now, so a replay sees one continuous "today" (whole-window loss cap) rather than per-day windows.
   - **Tests:** 15 in `test_backtester.py` (exact-number buy/sell PnL with live sell-slice sizing, stop/take-profit auto exits + disable switch, gate rejects, fee flows into net PnL and equity, benchmarks, multi-symbol shared book, unpriceable-decision skip, determinism) + 5 `CCXTProvider.fetch_history` pagination tests, 3 stocks range tests (incl. yfinance window padding via fake frames), 4 `get_decisions_in_range` tests. **380 tests passing, 95% coverage.**

### §7.15 — Dashboard + control (Docker WebUI) — ✅ complete (P1–P5)

Locked design: ARCHITECTURE.md "Data pipeline, storage & dashboard" — FastAPI + Jinja2/HTMX + uPlot via CDN (no Node build); control scope Pause/Resume + Close-all + safe config editor only (no manual orders, no kill); **no credentials ever** — never read, written or returned; one SQLite WAL DB as the control source of truth via `agent_control`.

   - **P1 — shared state ✅:** WAL (§7.15-preq) + `agent_control` table (`state`/`close_all_requested`/`last_cycle_at`/`last_error`/`config_override_json`, one row per agent) with repository upserts in `storage.py`. `BaseTradingAgent._handle_control` runs at the top of every cycle: overrides → close-all (executes **even while paused**, via `DecisionPipeline.close_all_positions` — no LLM, no risk gate; latch cleared after attempt) → pause skip; heartbeat stamped after each normal cycle. All checks strict (`is True` / equality), read fail-soft — a broken control plane never halts trading nor fabricates actions.
   - **P2 — agent-side control API ✅:** `src/core/control_api.py::create_control_app`, started in-process by `core/runner.py` when `control_api.enabled` (loopback-bound, off by default; ports crypto 8101 / stocks 8102). `GET /api/agents[/{agent}]`, `/decisions`, `/portfolio`; `POST pause|resume|close-all` (DB latches only — never orders); `GET/PUT /api/config` accepting only `SafeConfigOverrides` (`extra="forbid"`), applied to live objects via `control_config.parse_and_apply` (`interval_minutes` applies at restart).
   - **P3 — dashboard monitor ✅:** `src/dashboard/` — `create_dashboard_app` + pure view-models (`views.py`: win-rate = wins/closed (None when nothing closed, never a misleading 0%), confidence histogram, uPlot columnar shaping, tolerant positions parsing) + Jinja2/HTMX templates. Overview (portfolio cards, portfolio-value chart via `/api/portfolio.json` polling), positions, decisions + stats, health cards polled at `dashboard.refresh_seconds`. Reads the shared SQLite DB (WAL) as a reader (`scripts/run_dashboard.py`). Tests: unit view-models + integration seeded-DB render.
   - **P4 — dashboard control + config ✅:** HTMX Pause/Resume/Close-all posting to `/control/{agent}/{action}`, writing the `agent_control` latches **directly** — no HTTP coupling to the agent process, works with or without `control_api.enabled`, honored next cycle and survives restarts. Safe-config form validated through `validate_overrides_payload` → `SafeConfigOverrides`; every submitted key passes through so an injected credential-shaped field rejects wholesale (form re-renders with the rejection); accepted values persist to `agent_control.config_override_json`.
   - **P5 — Docker packaging ✅:** `Dockerfile` (python:3.11-slim, non-root `appuser`; image installs the package itself + `[stocks]`, dashboard templates ship as package-data via explicit setuptools discovery in `pyproject.toml`) + `docker-compose.yml`: `agent-crypto`, `agent-stocks`, `dashboard` (loopback-only host mapping `127.0.0.1:8080:8080`, `/healthz` healthcheck) and `backtester` under a `tools` profile for on-demand `docker compose run`. Shared named volume `agent-data` holds the SQLite WAL; **deviation from the locked design:** `./config` is a read-only bind mount instead of an `agent-config` named volume — nothing writes config files (safe overrides live in DB rows), so host edits stay authoritative on restart rather than going stale in a pre-seeded volume. Secrets only via compose environment substitution (`${KRAKEN_API_KEY:-}` etc.; empty → paper executor); `.dockerignore` guarantees `.env` is never baked into an image; LM Studio reached via `host.docker.internal:host-gateway` (`LM_STUDIO_ENDPOINT` overridable). Verified: image build, dashboard container `/healthz` + page render, crypto-agent `--once` cycle inside a container writing to the shared volume (safe fallback HOLD with no LLM present), clean exit.
   - **Acceptance met:** from a browser — live portfolio value/decisions/health; pause & resume visible; close-all executes even while paused; safe config edits take effect next cycle. Credentials structurally absent everywhere.

### §7.16 — XTB demo execution via xAPI — ✅ complete (and the "OAuth2" premise was wrong)

   - **Reality check first:** the item said "OAuth2 flow" — it doesn't exist. The old hosts (`ws.xtb.com`/`xapi.xtb.com`) were retired 2025-03-14; xAPI now lives on `wss://ws.xapi.pro/{demo,real}` and authenticates with its classic `login` command (account id + the **xAPI verification code** generated in xStation, ~30-day validity). Verified against maintained wrapper libraries (see `nightly_finds.md` #13) — there is no token endpoint.
   - **Done ✅:** `src/execution/xtb_client.py::XApiClient` implements the existing `XTBClient` seam over ordered JSON transactions: lazy connect + login, instant orders (`tradeTransaction` type=OPEN, cmd BUY/SELL at the passed mark) with `tradeTransactionStatus` polling mapped to filled/pending/rejected, venue rejections returned as a rejected `OrderResult`-shaped dict instead of exceptions, positions from `getTrades(openedOnly)` marked live via `getTickPrices` (bid for longs, ask for shorts), balance from `getMarginLevel`, DELETE transaction behind `cancel_order`, reconnect-once with retry, idempotent logout+close. The socket sits behind an `XTBTransport` protocol so every branch is unit-tested with a scripted fake — zero network in tests; `websockets` is imported lazily.
   - **Done ✅ (opt-in wiring):** the stocks runner builds `XTBExecutor(XApiClient)` only when `xtb_execution.enabled` AND both `XTB_ACCOUNT_ID`/`XTB_ACCOUNT_PASSWORD` are set; anything missing → paper stays with a warning naming what's absent. New validated `xtb_execution` config block (`account_type: demo|real` rejected otherwise); deliberately **not** in the dashboard safe-config whitelist — enabling real execution must stay a file+env change.
   - **Known limitations (documented, accepted):** volume is xAPI *lots* (≈1 share/lot for XTB equities; symbol specs not validated), fills tracked gross of commission (trade records carry fees, `create_order` doesn't — §7.8 precedent), and the streamer channel isn't subscribed — marks come from one-shot `getTickPrices` per positions fetch.
   - **Tests:** 17 in `tests/unit/test_xtb_client.py` (login ordering/args + failure, order payload shape buy/sell, spec-price fallback, rejection dict, poll-to-terminal, position marking incl. malformed records, reconnect/retry once then raise, logout-once, account_type/url guards) + 3 executor-selection tests in `test_run_stocks_agent.py`. **456 tests passing, ~93% coverage.**

### §7.27 — Clock injection for RiskEngine (backtest replay fidelity) — ✅ complete [R2-1.2, R3-M1, find #10]

   - **Done ✅:** new `Clock` Protocol + `SystemClock` default in `risk_engine.py`; `DailyLossTracker`, `ConsecutiveLossTracker` and `RiskEngine.__init__` accept an optional clock (live paths unchanged — still `datetime.now(UTC)`). `DecisionReplayBacktester` now owns a `TimelineClock` that follows each candle/decision timestamp, and feeds every event's equity through `update_daily_value` (live parity with `BaseTradingAgent`), so multi-month replays get real per-day loss windows and market-time cooldowns instead of one continuous wall-clock "today".
   - **Tests:** `TestClockInjection` in `test_risk_engine.py` (day rollover and cooldown expiry driven purely by a fake clock; default engine keeps live behavior) and `test_daily_loss_cap_resets_when_replay_day_advances` in `test_backtester.py` — an exact-numbers regression that pre-§7.27 rejected the third buy under a cumulative whole-window cap. 504 tests passing.

### §7.29 — Test suite deprecation & async-mock warnings resolved — ✅ complete [R3-M2]

   - **Done ✅:** `pytest` now runs with **zero warnings** (was 19). Root causes: (a) test helpers back-dating rows via raw `text("UPDATE ... SET timestamp = :old")` bound Python `datetime` params, which fall through to sqlite3's default datetime adapter (deprecated since 3.12); they now bind the string in SQLAlchemy's SQLite DATETIME format (`%Y-%m-%d %H:%M:%S.%f`) — typed ORM statements were never affected; (b) `_agent_with` in `test_control_plane.py` mocked `RiskEngine.update_daily_value` (a *synchronous* method) with an auto-created `AsyncMock` attribute, so the sync call produced an unawaited coroutine; the mock now pins a `MagicMock` mirroring the real signature. No production code changes were needed — both were test-side artifacts.
   - **Files:** `tests/unit/test_storage.py` (two helpers), `tests/unit/test_control_api.py`, `tests/unit/test_control_plane.py`. 504 tests passing, no warnings.

### §7.30 — Update stale XTB executor protocol documentation — ✅ complete [R3-M3, find #13]

   - **Done ✅ (docs):** `execution/xtb_executor.py` module + `XTBClient` Protocol docstrings no longer describe an OAuth2 blocker that never existed and a "until the real client lands" state — §7.16 shipped `xtb_client.py::XApiClient` (WebSocket xAPI, classic `login` auth) and the opt-in runner wiring; the docstrings now point at that reality (hosts, verification-code auth, `xtb_execution.enabled` + env-credential gate, paper still default).

### §7.31 — End-to-end Control API ↔ Agent loop integration test — ✅ complete (and it caught a live bug) [R2-3.1, find #16]

   - **Done ✅:** new `tests/integration/test_control_loop.py` drives the **real** `create_control_app` FastAPI surface (in-process httpx ASGITransport) against a **real** `CryptoAgent` — real pipeline (only provider + LLM mocked), real `RiskEngine`, real `PaperExecutor`, real SQLite. Pins: HTTP pause halts cycles (no new decision rows), resume restarts them, `close-all` closes positions on the next cycle and clears its latch with the closing sell persisted + outcome backfilled to the entry decision, close-all executes even behind a pause latch, and the agent heartbeat is visible on the very row the API reads.
   - **Bug found & fixed (nightly_finds #16):** `CryptoAgent`/`StocksAgent` passed `component="crypto_agent"`/`"stocks_agent"` to `BaseTradingAgent`, keying their `agent_control` row under names **no consumer used** — the runner, control API and dashboard all speak `crypto`/`stocks`. In production this meant pause/close-all latches were never read and heartbeats landed on orphan rows (dashboard health permanently `offline`). Unit tests missed it because they construct `BaseTradingAgent` directly with matching names. The subclasses now use the runner component names, and these integration tests pin the shared key.
   - **Tests:** 5 new integration tests; suite at 509 passing, zero warnings.

### §7.32 — APScheduler concurrency, misfire and overlap tests — ✅ complete [R2-3.2]

   - **Done ✅:** `TestRealSchedulerSemantics` in `tests/unit/test_scheduler.py` pins the behaviors `AsyncSchedulerManager.schedule_cycle`'s policy kwargs (`max_instances=1`, `coalesce=True`) promise, against a **real** `AsyncIOScheduler` on the test loop: a slow job outliving several ticks never overlaps itself yet later ticks still run; misfires during a long block collapse to a trickle instead of one replay per missed tick; a raising job keeps being scheduled (job survives its own errors); and a failing trading-cycle job does not disturb a second job (storage prune) on the same scheduler — mirroring the runner's two-job setup.
   - **Tests:** 4 new real-scheduler tests + a manager policy-kwargs assertion. 514 passing; timing margins verified stable over repeated runs.

### §7.33 — LLM seed parameter + response size guard — ✅ complete [R2-2.2, R2-2.4]

   - **Done ✅:** `LLMSettings.seed` (`int | None`, shipped as `seed: null` in `settings.yaml`) is sent with every chat request when set — the determinism knob for evaluating one model version repeatably (pairs with §7.19's `temperature`/`max_tokens`); omitted entirely when unset so provider defaults apply untouched.
   - **Done ✅:** `llm.max_response_chars` (default 20 000, `0` disables) caps the raw completion length in `llm_client.py` *before* parsing — a runaway/degenerate generation fails the attempt like any other error (retry with backoff → safe HOLD fallback), never reaching `_parse_signal`. Deliberately **not** on the dashboard's `SafeConfigOverrides` whitelist.
   - **Tests:** `TestSeedAndSizeGuard` in `test_llm_client.py` (seed omitted by default / sent when set; oversized response rejected on every attempt ending in a fallback HOLD naming "too large"; guard-off passthrough) plus shipped-config assertions in `test_config.py`. 518 passing.

### §7.37 — Calendar-aware Sharpe annualization, O(N) MACD, daily-loss epsilon — ✅ complete [R3-L3, R3-L4, find #3, find #11]

   - **Done ✅ (Sharpe):** new `backtester.py::estimate_periods_per_year` infers the equity curve's realized cadence (points ÷ span-years), so daily *stock* series annualize near ~252 trading periods instead of the nominal 365 that inflated stock Sharpes against crypto; falls back to the nominal timeframe table when the curve is too short (<10 points), degenerate, or >2× implausible.
   - **Done ✅ (MACD O(N)):** `_compute_macd` no longer re-slices and recomputes both EMAs per prefix (O(N²) on 6-month stock histories); a new `_ema_series` walks each period once — `out[k]` *is* the EMA of `values[:k+1]`, so results are **bit-identical** to the old loop (pinned against a verbatim reference implementation).
   - **Done ✅ (epsilon):** `_check_daily_loss` compares with a relative epsilon biased toward rejection — a drop float-rounding lands a hair short of the cap is still rejected; the guard never became more permissive.
   - **Tests:** new `tests/unit/test_indicator_math.py` (oracle-identity MACD incl. threshold/short cases, series-vs-prefix EMA equality, stock/crypto/short-curve annualization) and `TestDailyLossEpsilon` in `test_risk_engine.py`. 528 passing.

### §7.35 — Automated point-in-time SQLite backups before pruning — ✅ complete [R2-4.1]

   - **Done ✅:** `Storage.backup(dest)` wraps SQLite's **online backup** API over aiosqlite (consistent even with WAL writers active), and `prune_storage` now runs an optional backup pass *before every prune* — timestamped `<backup_dir>/<db-stem>-<UTC stamp>.db`. Config: `storage.backup_dir` (shipped as `data/backups`, empty disables) and `storage.backup_keep` (rotation of the oldest, shipped at 14; 0 keeps all — rotation only ever touches files matching this DB's own prefix in that directory). Backup failures are fail-soft and never block pruning; the whole pass stays crash-safe as before.
   - **Tests:** `TestDatabaseBackup` in `test_retention.py` — backup precedes deletion with the pruned row recoverable from the snapshot file, disabled-config writes nothing, rotation keeps only the newest N, and a failing backup still lets the prune run. 532 passing.

### §7.38 — Short-side position model & multi-side FIFO tracking — ✅ complete [R3-L2, find #4, find #9]

   - **Done ✅ (model):** `Position.side` (`PositionSide` StrEnum, default `long`) with absolute `quantity`; `pnl`/`pnl_pct` invert for shorts and `PortfolioState.total_value` carries short exposure as a liability at the close price. Defaults keep every spot path and every stored snapshot working untouched (side round-trips through portfolio JSON).
   - **Done ✅ (tracker):** `PositionTracker` gains an explicit second ledger — `open_short()` / `cover()` with the same FIFO, fee pro-rating and `closed_entries` decision attribution as the long book; plus `short_quantity()`. Sides are *never inferred from buy/sell verbs* (find #9): direction is chosen by the executor, so a spot sell can't accidentally open a short.
   - **Done ✅ (venues, find #4):** `KrakenExecutor.get_positions` now maps ccxt shorts honestly — negative contracts *or* an explicit `side: "short"` payload → `PositionSide.SHORT` with absolute quantity, never a positive-quantity long. `XTBExecutor`/`XApiClient` likewise carry xAPI's direction (opening `cmd`) through the position payload.
   - **Scope note:** groundwork for margin/derivatives — no executor opens shorts today, and the risk engine still guards the spot book only; wiring short *trading* into signals/risk remains future work.
   - **Tests:** short-side classes in `test_models.py` (pnl inversion, liability valuation, JSON round-trip), `TestShortSide` in `test_position_tracker.py` (profit/loss, FIFO attribution across lots, fee splits, untracked cover reports nothing, independent sides on one symbol), venue mapping tests in `test_kraken_executor.py` / `test_xtb_executor.py`, and a client payload assertion in `test_xtb_client.py`. 545 passing.

### §7.36 — Storage repository sub-module decomposition — ✅ complete [R2-4.2]

   - **Done ✅:** `core/storage.py` became the package `core/storage/` with focused sub-modules — `models.py` (Base + all row models + `_as_naive_utc`), `engine.py` (`StorageBase`: engine construction/WAL/ALTER-TABLE migrations/`backup()`/`_session`), `snapshots.py` (market + portfolio snapshot mixins), `decisions.py`, `orders.py`, `control.py` (control-plane latches), `pruning.py` (`prune`). All method bodies moved **verbatim**.
   - **Facade kept:** `Storage` is composed from the mixins in `storage.py` and re-exported from `core/storage/__init__.py` together with every row model — every existing import (`from src.core.storage import Storage, AgentControlRow, …`) works unchanged; zero behavior change.
   - **Tests:** no new tests needed (pure decomposition); full suite green through the new import paths. 545 passing, ~94% coverage.

## Completed §7 items — D. Low severity / housekeeping (§7.17–§7.19, §7.35–§7.38)

### §7.17 — Split `analysis/` out of `core/decision_pipeline.py` — ✅ complete

   - **Done ✅:** `src/analysis/indicators.py` now hosts `compute_indicators` and every private helper (`_compute_rsi`, `_compute_macd`, `_compute_bollinger_bands`, `_compute_atr`, `_sma`, `_ema`) moved **verbatim** from `core/decision_pipeline.py`; `src/analysis/prompt_builder.py` hosts `build_user_prompt` (+ `_format_outcome`) and the system prompt, renamed to a public constant `DEFAULT_SYSTEM_PROMPT`.
   - **Done ✅:** `DecisionPipeline` keeps its constructor signature (`system_prompt: str | None = None`, defaulting to `DEFAULT_SYSTEM_PROMPT`) and imports from `..analysis.*`; all call sites (`core/backtester.py`, agents, tests) updated. Pure housekeeping — no behavior change, indicator math and prompt text byte-identical.
   - **Tests:** no new tests needed; the existing pipeline/indicator/prompt suites exercise the moved code through its new import paths unchanged. **456 tests passing, ~93% coverage.**

### §7.19 — Code nits bundle [R-L] — ✅ complete

   - **Done ✅ (encapsulation):** `DecisionPipeline.get_portfolio_state` is now public (was `_get_portfolio_state`, called privately from both agents *and* tests); `ConsecutiveLossTracker.cooldown_until` is a public property and `_check_cooldown` reads it instead of poking `_cooldown_until` behind a `# type: ignore`. Call sites in `agents/base_agent.py` and the test contract updated.
   - **Done ✅ (honest state):** `DailyLossTracker._latest_value` is declared in `__init__` (`float | None`) — the `getattr(self, "_latest_value", None)` hack is gone; `daily_pnl_pct` guards against a missing latest value.
   - **Done ✅ (config-driven):** new settings with shipped defaults identical to the old constants — `risk.consecutive_losses_threshold` (was hardcoded 3 in the tracker), `llm.temperature` / `llm.max_tokens` (were hardcoded in the request payload), `llm.retry_backoff_base_seconds`. Paper `initial_cash` was already config-driven (§7.7); the sell-size cap (`min(quantity, held)` in `calculate_quantity`) and epsilon-based dust cleanup (`<= 1e-12`, never float `== 0`) were verified already-fixed — the nit's remaining claims were stale.
   - **Done ✅ (LLM client):** retries now sleep with exponential backoff (`base × 2^(attempt-1)`, off after the final attempt, `retry_backoff_base_seconds: 0` disables); the fragile `endpoint.rsplit("/v1", 1)[0]` + re-append dance is replaced by `_resolve_chat_url` (accepts `.../chat/completions`, `.../v1`, or bare host) resolved once at construction.
   - **Done ✅ (logging & types):** `risk_engine.py` logs via structlog like the rest of the codebase (`llm_client.py` already did); rejection event is `risk_rejected` with `symbol`/`reason` kwargs. `PipelineStep(str)` → real `enum.StrEnum`. RSI/ATR docstrings + module note state the simple-average (non-Wilder) methodology so LLM-facing numbers aren't misread against TradingView/pandas-ta.
   - **Tests:** 9 new — `daily_portfolio_value` None-before-update, public `cooldown_until`, config-driven threshold via `record_outcome`, chat-URL resolution (4 endpoint shapes), absolute-URL posting, exponential backoff sequence (`[0.5, 1.0]`). **465 tests passing, ~93% coverage.**

## Completed §7 items — E. Done (for the record)

The original PLAN.md §7.E block ("Done (for the record)") — the pre-review deliverables that closed external-review follow-ups:

### §7.20 — Close the "learn from its own track record" loop — ✅ complete *(the plan's core differentiator)*

   - **Done ✅:** `build_user_prompt()` fetches the last N prior decisions via `get_recent_decisions()` and renders them under `CONTEXT:` (action, confidence, reasoning, risk verdict, timestamp); config-driven via `decision_history_limit` (default 10, 0 = off). The LLM now sees *what it decided and why it passed risk*.
   - **Done ✅:** the *outcome* half. `llm_decisions` now has a `realized_pnl` column (idempotent `ALTER TABLE` migration for pre-existing DBs). On a **closing** order (a fill that reports a non-`None` `realized_pnl`), the agent backfills it via `Storage.set_realized_pnl(decision_id, …)` (fail-soft), and links the order to the decision (`orders.decision_id`). It is surfaced through `DecisionRecord.realized_pnl` and rendered per prior decision as `outcome: +X (win)` / `outcome: X (loss)` / `outcome: still open`. This is what actually delivers §4.1's fine-tuning loop.
   - **Done ✅ (Net-of-fee):** the PnL the LLM is shown is the *net* figure — `PaperExecutor` deducts per-side commission from cash and from `realized_pnl` (see §7.22), so the feedback loop reflects real economics rather than optimistic fee-free numbers. The risk engine's win/loss also keys off this net value (a fee-eaten-flat trade counts as a loss, matching the §4.3 "win rate > 50% after fees" gate). Covered by `TestFees` in `tests/unit/test_paper_executor.py` (229 tests passing).
   - **Caveat (review [R-M2/M3]):** complete *for paper mode*, but outcome attribution and fallback-row gaps make the loop half-blind — tracked as §7.8.

### §7.21 — Get the crypto agent running on real data — ✅ complete (Week 4)

   - **Done ✅:** `scripts/run_crypto_agent.py` now always fetches **live public market data** via CCXT (`create_ccxt_provider(exchange_id=…, testnet=False)` — Kraken's public OHLCV endpoint needs no API key and no sandbox mode). Execution stays safe by default: no `KRAKEN_API_KEY` → `PaperExecutor` (fee + slippage aware, configured from `execution.paper_fee_pct` / `execution.paper_slippage_pct`); key set → `KrakenExecutor` on a **separate**, keyed, sandboxed client. `ImportError` from the lazy `ccxt` import fails fast with an actionable install hint. Covered by `TestBuildDataAndExecution` in `tests/unit/test_run_crypto_agent.py` (229 tests passing).
   - **Note:** the live smoke test could not be run in the development sandbox (outbound HTTPS to `api.kraken.com` blocked — environmental, not a code defect). Run `python -m scripts.run_crypto_agent` in a network-enabled environment to confirm the first cycle's logged decision. **Done ✅:**

### §7.22 — Add fee modeling to `PaperExecutor` — ✅ complete (correctness, *not* just a verification)

   - **Done ✅:** `PaperExecutor.__init__` now takes `fee_pct` (default `0.0`). On a **buy** it deducts `cost + fee` from cash (and rejects if there isn't room for the fee); on a **sell** it credits `proceeds − sell_fee` and computes **net** `realized_pnl = gross − buy_fee − sell_fee`. The LLM-facing PnL and the §4.3 *"win rate > 50% after fees"* gate are now comparable to real economics.
   - **Config-driven:** `execution.paper_fee_pct` (0.26%/side, a typical crypto taker) and `execution.paper_slippage_pct` (0.1%) in `config/settings.yaml`, loaded via the new `ExecutionSettings` (old configs without the block default to a fee-free venue). Both entry scripts wire these into the paper executor.

### §7.23 — External review fixes (earlier round) — ✅ verified; remaining deferred items now live as §7.5 (drawdown) and §7.8 (realized-PnL backfill)

   - **✅ Fixed — Timezone (market-hours guard was 1–2h off on a UTC host).** `StocksAgent.run_cycle` compared `datetime.now(UTC)` against a *Warsaw-local* window (e.g. `"09:00-16:30"`), so on a UTC host the guard ran the WSE window 1–2h off (CET/CEST). Fixed at the **call site** (not `is_market_open`, whose wall-clock contract is locked by `tests/unit/test_stocks_market_hours.py`): new `StocksAgent._local_now()` localizes `datetime.now(UTC)` to the market zone. The zone is **config-driven** — new `stocks_agent.market_timezone` (default `Europe/Warsaw` via `DEFAULT_MARKET_TIMEZONE`), wired through `AgentConfig` + `settings.yaml` + the runner; an unknown zone degrades gracefully to UTC. Covered by `TestLocalNowTimezone` (a UTC-host timestamp now maps into/out of the Warsaw window).
   - **✅ Fixed — SQLite concurrent writes (``database is locked`` under concurrent agents).** Added `PRAGMA journal_mode=WAL` in `Storage.initialize()` (`_enable_wal`), applied on the same sync engine used for `create_all`. WAL is a **persistent property of the SQLite file**, so it covers the async engine and every subsequent connection (dashboard/backtester readers). Covered by `test_initialize_enables_wal` (asserts `PRAGMA journal_mode` = `wal` through the async engine). This is P1's first sub-step of §7.15 and lands ahead of the dashboard.

## Completed §7 items — F. Feature additions (§7.24)

### §7.24 — Start/stop agents from the dashboard (opt-in process supervision) — ✅ complete

- **Ask:** drive the whole system from the browser — a **Start** button that brings up an agent runner, not just its latches. This deliberately bends the §7.15 rule that the dashboard never touches processes, so the surface is opt-in and minimal.
- **`src/dashboard/launch.py::AgentLauncher`:** spawns the *same entry points you'd run by hand* (`python -m scripts.run_<agent>_agent`) via `asyncio.create_subprocess_exec`; child stdout/stderr append to `data/agent_<name>.out.log`; pid persisted to `data/<agent>.pid`. Children deliberately **outlive the dashboard** (killing the UI must never halt trading); a restarted dashboard **re-adopts** its old children only when the pidfile's pid is alive *and* its `/proc` cmdline still matches the expected runner module — a recycled/foreign pid is never killed, and stale pidfiles are pruned. `stop()` SIGTERMs (10s grace, then kill) **only managed processes**; anything started elsewhere can be paused via latch but never killed from here.
- **Config gate:** new `dashboard.allow_launch` (default **false** in code; enabled in the repo's `settings.yaml`). Under docker-compose keep it false — services there belong to compose. Not on the safe-config whitelist (launching processes is never a form edit applied at runtime).
- **Endpoints:** `POST /launch/{agent}/start|stop` → refreshed health fragment. Start refuses (409) when the agent is disabled in config, when its heartbeat is fresh (an agent is already trading — no twins), or when already managed; Stop refuses (409) when nothing is managed. 403 wholesale when supervision is off.
- **UI:** health cards gain **Start** (offline + enabled agents) and **Stop (pid …)** buttons plus an honest pidfile/managed view; the runner's own enabled-gate, risk rules and paper-by-default execution apply unchanged to launched agents.
- **Heartbeat semantics tightened for trust (§7.24 prerequisite):** paused agents now stamp the heartbeat too (`BaseTradingAgent._handle_control`), and `views.py::agent_status` checks freshness *first* — a stale beat reads `offline` even behind a `paused` latch, so "alive but paused" and "died while paused" are distinguishable and Start can't double-launch.
- **Log viewer (same item):** dashboard-launched runners' captured output is browsable at `/logs/{agent}` — last ~64 KiB / 400 lines, HTMX-polled refresh, `Logs` link on the health card once a log exists; read-only and path-fixed to `data/agent_<name>.out.log` next to the live DB (new `Storage.database_path` accessor makes that the single source of truth).
- **Tests:** 8 unit (`tests/unit/test_dashboard_launch.py` — real sleep-subprocess lifecycle, adoption via `/proc` match, foreign-pid never killed, stale pidfile pruning) + 5 integration (`TestLaunch`/`TestLaunchDisabled` in `tests/integration/test_dashboard.py`, injected sleep launcher — round trip, fresh-heartbeat refusal, disabled agent, 403 when off). Live smoke: dashboard Start spawned a real crypto runner that completed cycles into the shared DB; Stop reaped it. Suite: **488 passing**.

## Completed §7 items — G. External review 4 (§7.39–§7.60)

Findings from `external_4.md` (2026-09-24), tagged `[R4-xx]`.

### §7.39 — Scope storage per agent — ✅ complete [R4-C1]

   - **Problem:** both runners share one SQLite file (compose: one `agent-data` volume), but `portfolio_snapshots`, `llm_decisions` and `orders` carried no owner. A crypto restart rehydrated whichever agent wrote the latest snapshot (the stocks book landed in the crypto `PaperExecutor`), the drawdown high-water mark was `MAX` over both agents (one agent's +5% permanently blocked the other's entries), and the daily-loss baseline, loss-streak rehydration, FIFO fill replay and the dashboard/control-API portfolio views all mixed agents.
   - **Done ✅:** an `agent` column (indexed, `String(20)`) on the three tables. `Storage(path, agent=...)` is now **agent-bound**: writes are stamped with the binding and every scoped read (`get_latest_portfolio_snapshot`, `get_first_portfolio_snapshot_of_day`, `get_max_portfolio_value`, `get_portfolio_history`, `get_recent_decisions`, `get_closed_decisions`, `get_decisions_in_range`, `get_filled_orders`, `get_recent_orders`) filters on it; an explicit `agent=` argument overrides, and an unbound Storage (dashboard, CLIs, tests) reads across agents. `run_agent` binds to its `component`; the control API passes its `agent_name` explicitly; `scripts/backtest.py` replays the crypto agent's decisions for `--provider ccxt` and the stocks agent's for `yfinance`. `market_snapshots` (symbol-keyed candle cache) and pruning stay global.
   - **Migration:** `StorageBase._apply_migrations` adds the column + `ix_<table>_agent` index to existing databases and backfills once — decisions/orders by symbol (`BASE/QUOTE` → `crypto`, tickers → `stocks`), portfolio snapshots by their positions (all tickers → `stocks`; anything else, including indistinguishable empty books → `crypto`, the default-enabled agent). Verified against a copy of the live DB.
   - **Dashboard:** overview, positions and `/api/portfolio.json` take `?agent=` (default: first configured agent) and show one agent's book; `/decisions` shows all agents (with an Agent column) or `?agent=`-filtered; unknown agents 404.
   - **Tests:** `TestAgentScoping` + `test_agent_column_added_and_backfilled` (`test_storage.py`); `tests/integration/test_shared_db.py` replays the review's scenario through the real rehydration path (own book restored, per-agent drawdown peak, per-agent fill replay and loss streak); `TestPerAgentBooks` (`test_dashboard.py`); runner tests assert `Storage(..., agent=component)`. 564 tests passing.

### §7.42 — Position-size cap per position, not per order — ✅ complete [R4-C4]

   - **Problem:** `_check_position_size` compared only the *planned order* notional with `max_position_pct × total_value`, and `_check_max_positions` only blocks *new* symbols — so repeated BUYs of a held symbol each passed. Reproduced: 12 approved BUYs pyramided BTC to **90% of equity** (it only stopped because fees/slippage made the cash-bound buy fail). ARCHITECTURE's risk table already promised "10% of portfolio **per symbol**".
   - **Done ✅:** new `risk_engine.long_exposure(portfolio, symbol)`. For BUYs the gate now (a) rejects outright when the symbol's existing long exposure is already at the cap ("Position in X already at max size"), and (b) checks `existing + planned_notional` against the cap (the rejection reason names what is already held). The shared `calculate_quantity` sizes a BUY to the **headroom** `cap − existing`, so live and the decision-replay backtester stay identical. SELLs and other symbols are unaffected; the cap is an entry rule measured at the mark (later appreciation may lift a position above it — no forced trimming).
   - **Tests:** `TestPerPositionCap` (`test_risk_engine.py` — at-cap rejection, overshoot rejection, exact-headroom approval, other symbols/sells untouched); `TestPositionHeadroomSizing` (`test_decision_pipeline.py` — headroom sizing and the review's 12-BUY reproduction now stopping at one fill ≈10%); Hypothesis `test_repeated_buys_never_exceed_the_position_cap` over random price sequences. 571 tests passing.

### §7.45 — Prompt carries the agent's own book; honest outcome labels — ✅ complete [R4-H3]

   - **Problem:** the user prompt showed candles, indicators and past decisions but *not* whether the agent held the symbol — no size, entry, uPnL, active SL/TP, cash or limit — so the LLM could not tell opening from adding (feeding the §7.42 pyramiding) and issued sells on flat symbols. Every HOLD or rejected decision rendered `outcome: still open` forever (100% of the real DB's history), telling the model it held trades that never existed.
   - **Done ✅ (book):** new `BookContext` model (`core/models.py`); `build_user_prompt(..., book=)` renders a **YOUR BOOK** section — "spot account, SELL closes, never shorts"; the symbol's LONG position (qty, avg entry, mark, unrealized PnL abs/%, share of equity, active stop/take-profit) or "none (flat) — a SELL has nothing to close"; cash vs total equity; the per-symbol limit with remaining headroom ("at the limit, a BUY will be rejected"). Sub-$1 prices keep significant digits. `DecisionPipeline` reads the portfolio **once, before the prompt**, and reuses that same snapshot at the risk gate; an unreadable portfolio now ends the cycle with an error result *before* the LLM call instead of raising out of the risk step.
   - **Done ✅ (outcomes):** `DecisionRecord.filled` (from new `Storage.get_filled_decision_ids`, fail-soft) and `_format_outcome(record)`: realized PnL → win/loss/flat; HOLD → `n/a (hold — no trade)`; rejected → `n/a (rejected — not executed)`; approved but unfilled → `n/a (order not filled)`; SELL without outcome → `n/a (no tracked position closed)`; only an executed, unclosed entry is `still open`.
   - **Tests:** `TestHonestOutcomeLabels`, `TestBookSection` (flat/held/at-limit/sub-dollar rendering, the pipeline hands the book to the LLM, portfolio-read failure skips the LLM call), `TestHistoryFillLookup` (storage + pipeline `filled` flags); the old test asserting a HOLD renders "still open" was corrected. 581 tests passing.

### §7.56 — Configurable timeframe; forming-bar handling; one LLM decision per closed bar — ✅ complete [R4-M7]

   - **Problem:** `CryptoAgent` `"1h"` / `StocksAgent` `"1d"` were hardcoded (against the config-driven rule), and with 5/15-minute cycles the LLM re-judged the same candles ~12×/~30× per bar — including the venue's *forming* last bar with partial volume and a provisional close — inviting repeated entries (§7.42) and wasting local-LLM time.
   - **Done ✅ (config):** `crypto_agent.timeframe` / `stocks_agent.timeframe` (defaults `1h` / `1d` when unset) flow through both runners to the agent; `run_agent` warns at startup when the cycle interval is shorter than the bar and per-bar gating is off.
   - **Done ✅ (forming bar):** new `src/analysis/candles.py` (`timeframe_delta`, `split_forming`, `bar_close_time`; candle timestamps are bar *open* times). Indicators are computed on **closed bars only**; the forming bar still supplies the live price (marking, exit levels, prompt "Current price … (live — current bar still forming)") and is tagged `[FORMING — bar not closed, values provisional]` in the prompt's candle list; the indicator header says "closed bars only". Prompt prices keep significant digits for sub-$1 assets.
   - **Done ✅ (one decision per bar):** `<agent>.decide_on_new_bar_only` (default **true**) → `DecisionPipeline(decide_on_new_bar_only=...)`. When the latest closed bar closed *before* the symbol's last real decision, the cycle returns `PipelineResult.skip_reason` ("awaiting new 1h bar …") without an LLM call — marking and SL/TP enforcement still run every cycle, so a 5-minute cadence keeps its exit monitoring while decisions happen once per bar. The last-decision time is kept in memory and read once from storage after a restart (no re-asking the same bar); an LLM-fallback HOLD does not consume the bar (retried next cycle). Unknown timing never skips.
   - **Tests:** `tests/unit/test_bar_timing.py` — timeframe parsing, forming/closed split (aware/naive/untimed/unknown timeframe), bar close time, forming-bar prompt labels, closed-bar-only indicators, once-per-bar gating, new bar re-asks, fallback retry, exits enforced while waiting, restart via storage, off-by-default pipeline, config defaults and shipped settings. 606 tests passing.

### §7.43 — Browser-safe dashboard & control API; tighten-only risk overrides — ✅ complete [R4-H1]

   - **Problem:** both apps were unauthenticated with no CSRF token and no `Origin`/`Host` validation. Loopback binding doesn't help against the operator's *own browser*: the dashboard parses urlencoded bodies itself, so a cross-site `<form method=POST>` (a CORS simple request) could loosen every risk limit through the "safe" whitelist, switch off `enforce_exit_levels`, latch close-all/pause or start agents; DNS rebinding bypassed loopback entirely. The shipped YAML also had `dashboard.allow_launch: true` against its documented default.
   - **Done ✅ (request guards):** new `core/web_security.py` — `install_request_guards` adds Starlette's `TrustedHostMiddleware` (every request, reads included, must carry an allowed `Host`: loopback names + the configured bind host unless wildcard + new `dashboard.allowed_hosts` / `control_api.allowed_hosts`) and `OriginGuardMiddleware` (state-changing requests with a foreign `Origin`, or `Referer` when Origin is absent, get 403; non-browser clients without either are unaffected). The dashboard additionally requires a **per-process CSRF token** on every write (`/control`, `/config`, `/launch`): pages embed it — HTMX sends it via `hx-headers` on `<body>`, the config form as a hidden `csrf_token` field (stripped before whitelist validation).
   - **Done ✅ (tighten-only):** `Settings.risk_baseline` keeps an untouched copy of the YAML risk limits (the live `settings.risk` is mutated by applied overrides). `validate_overrides_payload(payload, baseline=...)` rejects any risk override looser than it (`max_position_pct`/`daily_loss_limit_pct`/`max_drawdown_pct`/`max_open_positions` ≤ YAML; `min_confidence`/`consecutive_losses_cooldown_minutes` ≥ YAML) in both the dashboard and `PUT /api/config`; `parse_and_apply` re-checks at apply time and skips stored values that became looser after a YAML tightening. `risk.enforce_exit_levels` is removed from `RiskOverride` and the form; legacy stored rows carrying it are read with the key dropped (warning) so their other overrides keep working. Shipped `dashboard.allow_launch: false`.
   - **Tests:** `TestBrowserSafety` in `test_dashboard.py` (token embedded, foreign Host → 400 on reads, cross-site Origin/Referer → 403 with no latch written, token-less/wrong-token writes → 403 on all three write routes, hidden-field token works, same-origin allowed, the review's attack payload refused, exit-level switch absent, tightening accepted) and `test_control_api.py` (cross-site close-all 403, foreign Host 400, curl-style clients unaffected, loosening PUT 400); `TestTightenOnlyOverrides` + shipped-config and `allowed_hosts` checks in `test_control_plane.py`. Test clients now use a loopback Host (+ the token for the dashboard). 631 tests passing.

### §7.44 — Fail-soft, lossless post-order persistence — ✅ complete [R4-H2]

   - **Problem:** `BaseTradingAgent._post_process` ran *outside* the per-symbol `try`, and `save_order` / `save_portfolio_snapshot` were unwrapped. A transient `database is locked` (dashboard and control API write the same file) after a successful fill lost the `orders` row — the §7.25 FIFO replay source — skipped the remaining symbols and the heartbeat. In reconciliation, `KrakenExecutor` had already dropped the pending order before the agent persisted it, so a failed write lost the transition for good.
   - **Done ✅ (agent):** `run_cycle` contains `_post_process` per symbol (last line of defense, recorded as the cycle error). Inside it every step is fail-soft: `_persist_order` retries the row write (`_persist_retry_delays`, default 0.2 s / 1 s) and, if it still fails, emits an `order_persist_failed` audit log line carrying the full row plus an error alert — the order is never silently lost; realized-PnL backfills, the portfolio snapshot and alert dispatch each log and continue.
   - **Done ✅ (reconciliation, two-phase):** `KrakenExecutor.reconcile_open_orders` caches a terminal result on the pending entry (`_PendingOrder.resolved`) and re-delivers it every cycle — without re-polling the venue or feeding the FIFO ledger again — until the agent calls the new `confirm_reconciled(order_id)` after persisting. `_reconcile_orders` confirms only after `update_order_status` (+ entry attribution) succeeded; when the stored row is missing it **re-creates** it from the venue's answer with the entry decision id (`pending_decision_id`).
   - **Tests:** `tests/integration/test_persistence_resilience.py` — transient write retried; permanent write failure keeps both symbols running, audit line + alert, heartbeat still stamped; snapshot failure and unexpected post-process errors contained; reconciled transition re-delivered after a failed write and polled once; lost row re-created with its decision; attribution applied exactly once. Kraken reconciliation tests updated for the confirm step. 638 tests passing.

### §7.46 — Loss streak counted once per closing fill, on every close path — ✅ complete [R4-H4, R4-L4]

   - **Problem:** live, `record_outcome` fires once per closing fill, but restart rehydration counted every *decision row* with a realized PnL — and an LLM round trip stamps PnL on both the SELL decision (`set_realized_pnl`) and its entry BUY (`add_realized_pnl`). Two losing trades rehydrated as a streak of 4 and re-armed a cooldown the live process never had. The dashboard's win rate double-counted the same way. Close-all fills and late reconciled fills never called `record_outcome` at all.
   - **Done ✅:** `orders.realized_pnl` (nullable; migrated onto existing DBs) stores the outcome of each *closing fill* — written by `_persist_order` and by reconciliation (`update_order_status(..., realized_pnl=)`, also on re-created rows). New `Storage.get_recent_closing_fills` (agent-scoped, newest first) is the rehydration source, so restart counting is exactly live counting; databases with no recorded closing-fill outcomes yet fall back to *entry* (BUY) decisions only. `DecisionPipeline.close_all_positions` and `BaseTradingAgent._reconcile_orders` now record outcomes like every other closing path. `views.decision_stats` counts one closed trade per entry decision (the SELL row repeating the exit's PnL is no longer a second trade).
   - **Tests:** `TestStreakMatchesLiveCounting` (two losing round trips → streak 2, no phantom cooldown; legacy entry-decision fallback) and `TestLiveOutcomeCoverage` (close-all records the loss) in `test_rehydration.py`, whose existing streak tests now seed closing fills; `TestClosingFillOutcomes` (agent round trip persists one closing fill and rehydrates as one loss; reconciled fill feeds the streak) in `test_persistence_resilience.py`; `TestClosingFills` (scoping/order/status filter, legacy migration) in `test_storage.py`; decision-stats expectations corrected. 645 tests passing.

### §7.47 — Exposure-reducing exits are never gated; SELL closes in full — ✅ complete [R4-H5]

   - **Problem:** an LLM SELL of a held long ran through confidence, daily loss, **drawdown**, **cooldown**, size and max-positions — once drawdown passed its limit (an effectively permanent latch) the LLM could never close a position; only SL/TP or a manual close-all could. That contradicted the §7.9 / close-all rationale the engine applied only to the stop-loss rule. SELLs were also sized as cap-sized *slices* (an appreciated position needed several sells), and a SELL on a flat symbol reached the executor as a doomed order.
   - **Done ✅:** `RiskEngine.evaluate` routes SELLs to `_evaluate_exit`: a SELL of a held long passes every exposure rule and is checked for **confidence only**; a SELL with no long position is rejected ("No open long position in X to sell — spot account: SELL closes a position, it never opens a short"). The shared `calculate_quantity` sizes a SELL to the **whole** long position (live and replay alike), matching the prompt's "SELL closes" contract (§7.45).
   - **Tests:** Hypothesis `test_exits_are_never_stranded` (any drawdown/daily loss/losing streak/position size → confident exit approved) and `test_flat_sell_is_refused`; `TestExitsCloseInFull` (full-position sizing; an LLM exit executes during an armed cooldown and flattens the book); cooldown property narrowed to entries; stop-less-close tests now close a held position; backtester expectations follow full-close sizing. Also fixed the duplicated risk-engine intro sentence in ARCHITECTURE.md (review L10). 649 tests passing.

### §7.51 — LLM outages visible; real alert channel — ✅ complete [R4-M2]

   - **Problem:** an LLM-fallback HOLD was not a cycle error — `last_error` stayed `None`, the dashboard showed `running`, and no alert fired. 56% of the stored decisions were fallbacks (`All connection attempts failed`) and nothing surfaced it. Alerts only ever reached a log sink, which used stdlib `logging` against the structlog rule.
   - **Done ✅:** `BaseTradingAgent.run_cycle` records a fallback as the cycle error ("LLM unavailable — fallback HOLD (…)") so the heartbeat carries it, and `_maybe_alert` sends a deduplicated `llm_unavailable` alert (severity error). Dashboard health cards show an **LLM fallbacks n/20 recent** badge per agent; the decisions page shows the fallback count and rate (`decision_stats.fallback_rate`). New `monitoring/alerts.py::WebhookAlertSink` — `json` format (`text` for Slack/generic, `content` for Discord, plus event/severity/message) or `ntfy` (plain body + Title/Priority/Tags headers), `monitoring.alert_min_severity` filter (default `warning`), failures return `False` and never raise. `build_alerts` always keeps the log sink and adds the webhook when **`ALERT_WEBHOOK_URL`** is set in the environment (the URL is a secret — never in YAML or the dashboard; added to `.env.example` and compose). The agent closes alert sinks on shutdown. Alerts now log via structlog.
   - **Tests:** `TestLLMOutageVisibility` (fallback → `llm_unavailable` alert + heartbeat error), `TestWebhookSink` (JSON/ntfy payloads, severity filter, failure → False, bad format), `TestBuildAlerts` (log-only without env, webhook from env, config validation) in `test_monitoring.py`; `TestLLMFallbackVisibility` (health badge + decisions rate) in `test_dashboard.py`. 659 tests passing.

### §7.49 — Backtester look-ahead bias removed — ✅ complete [R4-H7]

   - **Problem:** ccxt/yfinance candle timestamps are bar *open* times, but `_build_timeline` fired each candle event at its open while using its **close** for marks, exit checks and decision pricing. A 10:00 decision on daily stock bars filled at that day's 16:30 close; on 1h crypto up to an hour ahead; stops fired on closes the live system could not yet have seen. Replay PnL was systematically optimistic.
   - **Done ✅:** candle events are placed at the bar's **close time** (`open + timeframe_delta(timeframe)`, reusing §7.56's `analysis/candles.py`); same-timestamp ordering still puts candles before decisions (a bar closing exactly at decision time is known). Decisions are therefore priced at the last *closed* bar, and a decision made before any bar closed is unpriceable (counted as risk-rejected, as before for missing prices). Unknown timeframes fall back to open times with a warning. `scripts/backtest.py` already passes its timeframe through.
   - **Tests:** `TestNoLookAhead` (intraday decision fills at the previous close, not the same-day close; a decision before any close is unpriceable; a stop fires when the breaching bar closes). The existing replay tests encoded the look-ahead through their candle helper — it now builds honest bars (each opens the day before its close is known), and the daily-loss window test uses 12h bars. 662 tests passing.

### §7.48 — Side-aware closes and exit levels — ✅ complete [R4-H6]

   - **Problem:** since §7.38 Kraken/XTB report shorts as `side=SHORT`, but `close_all_positions` and `_check_exit_levels` always sent a SELL and `exit_level_breach` was long-only — closing a short *added* to it, and a short's stop (above entry) read as a take-profit. Reachable on XTB through §7.40.
   - **Done ✅:** `exit_level_breach` is side-aware (a short stops out at/above its stop and takes profit at/below its target; longs unchanged), new `closing_side(position)` returns SELL for a long and a covering BUY for a short, and both close-all and exit enforcement use it. `_check_exit_levels` now evaluates **every** position in the symbol (a hedged long + short is possible on a venue) and closes the first breaching leg.
   - **Tests:** `TestSideAwareCloses` in `test_decision_pipeline.py` — breach semantics for long/short (including a mark that would have fired the long rule on a short), close-all covers shorts and sells longs, a short's stop is covered with a BUY, a hedged book closes only the breaching leg. 671 tests passing.

