# Project History & Delivered Work

The delivery record of the autonomous trading agent project: what has been built, when, and how it was verified. Extracted from `PLAN.md` on **2026-09-17**, when PLAN.md became a gaps/todos-only document (see [PLAN.md](PLAN.md)); architecture content lives in [ARCHITECTURE.md](ARCHITECTURE.md).

**Reading conventions**

- **§7.N identifiers are never renumbered.** Completed items from PLAN.md §7 appear below under their original numbers; cross-references from code comments, `AGENTS.md` and `README.md` keep resolving.
- "Phase N" / "Week N" headings reproduce the original planning timeline. Phase 3 (risk/monitoring) design now lives in [ARCHITECTURE.md](ARCHITECTURE.md); Phase 4 (iteration/live-readiness) is still open and stays in [PLAN.md](PLAN.md).
- `[R-xx]` severity tags reference `review.MD` / `review2.md` at the repo root.

## Status snapshot

> ### Status (as of this revision)
> **Built & tested (436 tests passing, ~93% coverage):** core (LLM client, risk engine, storage, scheduler, decision pipeline with inline indicators + prompt), crypto provider + executor + agent, **stocks provider (xAPI + yfinance) + executor + agent**, paper executor, monitoring (structured logging), both entry scripts, the **"learn from its own track record" loop** (the LLM sees each prior decision **and its realized PnL outcome** — now attributed FIFO to the *entry* decision through the shared `execution/position_tracker.py`, on paper and on real venues alike — §7.8), **fee modeling in the paper executor**, the **crypto agent running on real data** (paper path fetches live public Kraken OHLCV via CCXT; execution stays simulated), the **timezone-aware market-hours guard**, **SQLite WAL mode**, **weekend/holiday-aware market-hours guard with wrap-around windows** (§7.10), **per-cycle position marking** (§7.1), **all seven risk rules live** including the peak-equity drawdown gate and the notional cap enforced at the gate (§7.5), a **hardened keyed-Kraken path** (real ccxt balance/fill payloads; spot `fetch_positions` degradation — §7.6, live testnet smoke still pending), **restart-safe paper portfolio + risk state** rehydrated from SQLite (`core/rehydration.py`, §7.7), **storage retention pruning** (startup + scheduled passes, `orders.created_at` for un-filled rows; portfolio snapshots exempt — §7.12), a **shared base agent + runner factory** (`agents/base_agent.py`, `core/runner.py` — the two agents and two scripts are now thin market-specific shells — §7.13), and **deterministic stop-loss / take-profit enforcement** (levels carried on positions; a breach closes the position on the next cycle without an LLM call and bypassing the gate — §7.9), and the **standalone web dashboard** (`src/dashboard/` + `scripts/run_dashboard.py`: FastAPI + Jinja2/HTMX monitor — uPlot portfolio chart, positions, decisions with win-rate/confidence stats, health cards — plus HTMX pause/resume/close-all controls and a safe-config editor, all writing the `agent_control` latches directly so they work with or without the agent-side control API — §7.15 P3/P4). Config-driven via `decision_history_limit`, the `execution:` block, `stocks_agent.market_timezone` / `market_holidays`, `risk.enforce_exit_levels`, and the `dashboard:` block (host/port/refresh/agents).
> **Not yet implemented (do not assume these exist):** news/sentiment feed, economic-calendar feed, `analysis/indicators.py` + `analysis/prompt_builder.py` (indicators & prompt currently live inline in `core/decision_pipeline.py`), XTB demo OAuth2 flow, Docker packaging for the agents + dashboard (§7.15 P5 — the control plane, control API and dashboard pages/config UI — §7.15 P1–P4 — have since landed; see ARCHITECTURE.md), and **venue-side** stop/take orders (our SL/TP checks are local to the agent). Full list incl. findings from the 2026-09-15 code review (`review.MD`): see §7 Gaps & Next Steps.

## Delivered milestones (original implementation order)

1. **Week 1:** `pyproject.toml`, config, `llm_client.py`, `storage.py` + tests ✅
2. **Week 2:** `risk_engine.py`, `decision_pipeline.py`, `paper_executor.py` + tests ✅
3. **Week 3:** `ccxt_provider.py`, `kraken_executor.py`, `crypto_agent.py`, `scheduler.py`, `run_crypto_agent.py` + integration tests ✅
4. **Week 4:** Monitoring (structured logging) ✅; **decision-history prompt wiring** ✅; **crypto agent paper mode on real data** ✅ (the paper path now fetches live public Kraken OHLCV — no API key required — and executes via the fee/slippage-aware `PaperExecutor`; Kraken testnet execution remains opt-in via `KRAKEN_API_KEY`)
5. **Week 5:** `xtb_provider.py`, `xtb_executor.py`, `stocks_agent.py` + tests ✅ (68 new tests added)
6. **Week 6:** **decision-replay backtesting** on fresh historical candles ✅ (§7.14); Docker dashboard packaging — open → PLAN §7.15 P3–P5 (design in ARCHITECTURE.md)
7. **Week 6 (cont.):** **control plane P1/P2** — `agent_control` table + per-cycle agent checks (overrides → close-all → pause, heartbeat) + agent-side FastAPI control API with the safe-config whitelist ✅ (§7.15; Docker packaging still open → PLAN §7.15 P5). Between the weeks above, every review finding landed in order: §7.1–§7.14 and §7.20–§7.23 (detailed write-ups below).
8. **Week 6 (cont.):** **dashboard P3/P4** — standalone FastAPI + Jinja2/HTMX web dashboard (`src/dashboard/app.py::create_dashboard_app`, pure view-models in `views.py`, `scripts/run_dashboard.py`): overview page with uPlot portfolio-value chart (`/api/portfolio.json` polling), positions, decisions + win-rate/avg-confidence/confidence-histogram stats, HTMX-polled health cards; Pause/Resume/Close-all buttons and a safe-config form writing the same `agent_control` latches as the control API (config validated via `validate_overrides_payload` → `SafeConfigOverrides`, credential-shaped keys rejected wholesale). Reads the shared SQLite DB (WAL) as a reader — no HTTP coupling to the agent process. 22 new tests (§7.15 P3/P4 ✅; Docker packaging → PLAN §7.15 P5).

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

## Completed §7 items — C. Medium severity (§7.8–§7.14)

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
