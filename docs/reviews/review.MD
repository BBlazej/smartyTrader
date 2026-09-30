# Project Review — Autonomous Trading Agent

**Date:** 2026-09-15
**Scope:** Full read of `src/`, `scripts/`, `tests/` (sampled), `config/`, `pyproject.toml`, `README.md`, `PLAN.md`, `AGENTS.md`, git history (4 commits, clean tree at `4e2f34e`).

**Verification performed:**

- `pytest` → **229 passed in 1.35s, 94% coverage on `src/`** — README claims confirmed. Suite passes with no network access (sandboxed), confirming "no real network calls in tests".
- `ruff check .` → clean. `ruff format --check .` → clean.
- `.venv` package inventory checked against `pyproject.toml` and `AGENTS.md` claims.

---

## Overall Assessment

This is a well-above-average early-stage codebase. The architecture is sound: protocol-based seams for providers/executors, a real deterministic risk gate between LLM and execution, persistence with a thoughtful migration path, and an unusually honest PLAN.md that already admits some of its own gaps. Test coverage is high and the tests are fast.

The main problem is a gap between **advertised safety/learning behavior and what actually runs**: several risk rules are no-ops, paper-mode portfolio valuation is frozen because position prices are never refreshed after entry, and restarts silently wipe all portfolio and risk state. None of these are bugs in the narrow sense — they are unfinished subsystems that current docs describe as if complete.

Findings below are ordered by severity. Items already acknowledged in `PLAN.md §7` are marked *(known)*.

---

## High Severity

### H1. Paper-mode position prices are never refreshed → portfolio valuation is frozen at entry

`PaperExecutor.update_price()` (`src/execution/paper_executor.py:205`) is **never called by any production code** — only by tests. After a buy, `Position.current_price` stays equal to the entry price forever (the docstring on `get_positions` — "Return current positions with updated prices" — is false).

Consequences that flow from this single gap:

- `unrealized_pnl` is always 0.0 in paper mode.
- `PortfolioState.total_value` never moves with the market between trades.
- **The daily-loss rule (H2 below) can only ever see fee/trade-flow drift, never actual market moves.** The headline `-daily_loss_limit_pct: 0.02` guard is effectively cosmetic in paper mode.
- Portfolio snapshots persisted for the future dashboard/backtester carry frozen valuations.

Fix direction: at the start of each cycle (or in `_post_process`), feed the latest close from the snapshot into `update_price(symbol, close)` for open positions — or make `PaperExecutor.get_positions` take a price map.

### H2. Two of the seven advertised risk rules are no-ops

README advertises "7 deterministic risk rules". Reality in `src/core/risk_engine.py`:

1. `_check_drawdown` (L179–183) — **unconditional `APPROVED` stub**. `risk.max_drawdown_pct: 0.05` is loaded from config and never used. *(known: PLAN §7 item "Deferred #2", with a sensible recommended fix — persisted high-water mark via `MAX(total_value)` over `portfolio_snapshots`.)*
2. `_check_position_size` (L155–162) — does **not check size at all**; it only rejects when total value ≤ 0. Actual sizing happens in the pipeline (`_calculate_quantity`, `decision_pipeline.py:249`) *after* the risk gate, so the gate never validates what was actually sized. If sizing logic ever regresses, nothing catches it.

Effective live rules are: confidence, max-positions, daily-loss (see H1 for why it's blunted), cooldown, stop-loss-required. The README/AGENTS "7 rules" claim overstates.

### H3. `enabled: false` still runs one full trading cycle

Both runners treat "disabled" as **"run one cycle — including order placement — then exit"** (`scripts/run_crypto_agent.py:155-161`, `scripts/run_stocks_agent.py:114-120`). A user who disables an agent to be safe will still get live orders placed (Kraken testnet, or paper) plus LLM calls and DB writes. At minimum this deserves a separate `run_once` flag; ideally `enabled: false` should exit without running anything.

### H4. The Kraken testnet path has never met real CCXT — likely broken where it matters

The executor is only tested against mocks (`tests/unit/test_kraken_executor.py`). Against the actual installed ccxt (4.5.78):

- `get_cash` calls `fetch_free_balance(code)` and does `float(balance)` (`src/execution/kraken_executor.py:147-149`). Real CCXT returns a **balance dict**, not a float → `TypeError` on every cycle once a key is set.
- `get_positions` relies on `fetch_positions()`. Kraken spot via CCXT does not provide positions (returns empty or raises `NotSupported`) → the max-open-positions rule and portfolio valuation will see nothing on the keyed path.
- The pipeline always passes `price=current_price`, so `place_order` **always sends a limit order at last close** (`kraken_executor.py:96`). A limit buy at the last close may never fill; status stays `"pending"` forever with no reconciliation, and `filled_at` is never set even for `"closed"`.

The paper path works; the keyed path needs an integration pass against real CCXT before anyone trusts "Kraken testnet mode".

---

## Medium Severity

### M1. All portfolio and risk state is in-memory — restart wipes it silently

`PaperExecutor` keeps cash/positions only in RAM; `DailyLossTracker` and `ConsecutiveLossTracker` likewise (`risk_engine.py:14-72`). Meanwhile decisions/orders persist in SQLite. After a restart: cash resets to 100k, positions vanish (open trades referenced by persisted decisions cease to exist), daily-loss baseline and cooldown reset to zero. Also `initial_cash` is not configurable — the 100k default is hardcoded (`paper_executor.py:20`). Fix directions: persist executor state in SQLite, or deterministically rehydrate from the orders table at startup; add `execution.initial_cash` to config.

### M2. The "learn from track record" loop attributes outcomes to the wrong decisions

On a closing sell, realized PnL is stamped onto **the sell's own decision row** (`crypto_agent.py:107-112`, same in stocks agent). The original **buy decision row stays `outcome: still open` forever**. So the context fed back to the LLM shows outcomes only for sell actions; every entry decision appears permanently unresolved. PLAN §7 marks this loop "complete" — it works mechanically, but as a learning signal it's half-blind. Fix: link orders→decision (`orders.decision_id` already exists) and backfill realized PnL onto the opening decision when its position closes (needs per-position attribution; overlaps with the deferred `PositionTracker` design in PLAN §7 item #4 — note that finding is *also* real: Kraken/XTB executors never set `realized_pnl`, so the loop no-ops on real venues).

### M3. A dead LLM pollutes its own future context

When all retries fail, `LLMClient` returns a HOLD fallback (`llm_client.py:117-122`) with reasoning "LLM unavailable after N retries". The agent **persists that as a normal decision** (risk verdict "approved"), and the next cycles feed it back into the prompt as one of the last-N decisions. After an outage, the model's context is filled with failure rows indistinguishable from genuine HOLDs. Fallback decisions should be marked (e.g. `risk_verdict: skipped` / a dedicated column) and excluded from history. Related: PLAN's own audit requirement ("log full prompt+response") is not implemented — only `signal.action` is logged.

### M4. Dependency drift between docs, pyproject, and the venv

- `pandas-ta` is declared in `pyproject.toml` but **never imported anywhere** (indicators are hand-rolled). Not installed in `.venv` either. Remove it (it also has known numpy-compat issues).
- `python-dotenv` is declared but never used — replaced by a duplicated hand-rolled `_load_dotenv()` in both scripts. Use the dependency or drop it.
- No dev-dependency group exists in `pyproject.toml`: pytest/pytest-asyncio/pytest-cov/ruff are installed ad-hoc and undeclared → no reproducible env. Add `[project.optional-dependencies].dev`.
- `AGENTS.md` claims property-based tests via `hypothesis` — **zero hypothesis tests exist** and it isn't installed. Same for `responses`. Update the doc or add the tests (risk-engine invariants would genuinely benefit).
- `yfinance` is not installed here → the stocks runner fails every cycle at fetch time (lazy import error surfaces per-cycle as "Fetch failed"). The README's optional-install hint exists, but a fail-fast hint (like the ccxt one) would be better.

### M5. No stop-loss / take-profit enforcement anywhere

Signals carry `stop_loss`/`take_profit`, the risk gate *requires* a stop on any active signal — and then nothing ever monitors open positions against those levels. In paper mode, losing positions can only be exited if the LLM happens to sell them. For a system whose thesis is realistic track-record learning, a simple deterministic exit check per cycle (close position when mark price crosses SL/TP) would materially improve PnL realism and bound drawdowns — especially while the drawdown rule (H2) remains a stub. This also depends on H1 being fixed (prices must be current for exits to trigger).

### M6. Stocks market-hours guard has no weekend/holiday awareness

`is_market_open` (`stocks_agent.py:53-63`) checks time-of-day only. On Saturday 10:00 Warsaw time the stocks agent runs happily on Friday's stale daily candles. WSE weekends (and holidays) are predictable; add a weekday check at minimum. Also note a config trap: if anyone specifies an overnight window (`22:00-08:00`), `start <= t <= end` is never true and the agent silently never runs.

### M7. Stocks data depth silently disables indicators

`YFinanceSource._PERIOD_MAP` requests `"1mo"` of daily bars (`xtb_provider.py:118`) ≈ 21 closes — below MACD's 26-close minimum and close to ATR/RSI thresholds. So the stocks prompt often lacks MACD entirely while crypto (100 candles) has it. Request more history (e.g. `"6mo"`). Related nit: `_f()` maps NaN→0.0, so a row with a missing Low becomes a 0-low candle that poisons ATR/BB — dropping the row would be safer.

### M8. Unbounded storage growth, no retention policy

Every cycle persists a full snapshot with ~100 candles as JSON per symbol (`crypto_agent.py:160-168`). At the configured 5-minute interval × 2 pairs that is ~58k snapshot rows/day (~30–60 MB/day). Decisions/orders/portfolio snapshots also grow forever. The Week-6 dashboard/backtester plans assume this DB stays usable — add a pruning job or retention config before it becomes an operational problem.

### M9. Near-total duplication between agents and runners

`crypto_agent.py` and `stocks_agent.py` are ~90% identical (cycle loop, post-process, persistence, alerts differ only in defaults); both scripts duplicate `_load_dotenv`, `_build_alerts`, and ~80 lines of wiring. The market-hours guard is the only genuinely agent-specific logic. Extract a shared base agent + a runner factory; every future change currently has to be made twice (and already drifted: stocks agent carries extra uncovered branches per coverage report).

---

## Low Severity / Nits

- **Encapsulation:** agents call `pipeline._get_portfolio_state()` (private) in both agents and tests; `_check_cooldown` reaches into `_loss_tracker._cooldown_until` with a `# type: ignore` (`risk_engine.py:187`).
- **Type lies:** `DailyLossTracker.daily_portfolio_value -> float` returns `None` via a `getattr` hack (`risk_engine.py:37-40`) — would crash `daily_pnl_pct` if read before any update.
- **Hardcoded values against project rules** ("config-driven, never hardcode"): consecutive-loss threshold of 3 (`risk_engine.py:67`), LLM `temperature=0.2`/`max_tokens=1024`, paper `initial_cash=100_000`.
- **LLM client:** no backoff between retries (3 immediate hits on a struggling local server); base_url derived via `endpoint.rsplit("/v1", 1)[0]` then re-appending `/v1/chat/completions` — works with the shipped config but fragile if an endpoint without `/v1` is set; exception handling collapses auth/4xx errors and JSON parse errors into the same retry path.
- **Logging split-brain:** `risk_engine.py` and `llm_client.py` use stdlib `logging`, everything else uses structlog. Stdlib records bypass the configured structlog renderers, so safety-critical risk rejections land in a different format than the rest of the log stream.
- **`PipelineStep(str)`** (`decision_pipeline.py:40`) is a str-subclass used as an enum stand-in — make it a real `Enum` like everywhere else.
- **Sell sizing:** `_calculate_quantity` sizes sells at `max_position_pct × total_value` regardless of units actually held → guaranteed-rejected sells (paper rejects them correctly) that can loop every cycle if the LLM keeps asking; consider capping by holdings or rejecting earlier with a clear reason in history. Float equality (`pos.quantity == 0`, `paper_executor.py:179`) for position cleanup can strand dust positions that count toward `max_open_positions`.
- **Doc/reality mismatches:** README quickstart says `cp .env.example .env` — **no `.env.example` exists**; AGENTS.md says docs live in `doc/`, actually `docs/`; venv runs Python 3.14 while docs say 3.11+ (fine, but the Docker plan pins 3.11-slim — verify deps build on what you actually run).
- **Indicator naming vs method:** RSI and ATR use simple averages of the last N deltas/TRs, not Wilder smoothing — values will differ from TradingView/pandas-ta readings. Acceptable simplification; worth a docstring note (partly present) so LLM-facing numbers aren't misinterpreted later.
- `PipelineResult` is documented "Immutable" but is a plain mutable class.

---

## What's Genuinely Good

- **Risk-gate architecture is correct by construction:** execution is impossible without passing `RiskEngine.evaluate`, HOLD short-circuits safely, and the LLM-fallback path degrades to no-trade rather than trade-on-error.
- **Protocol-based seams** (Executor/MarketDataProvider/AlertSink/XTBClient) with lazy imports keep the package importable and tests network-free — verified by the offline test run.
- **Storage craft:** WAL enabled correctly with a documented rationale, idempotent `ALTER TABLE` migration for the new column, fail-soft PnL backfill.
- **Alerts module** is nicely done: per-event dedup under a lock, per-sink failure isolation, injectable clock.
- **The timezone market-hours fix** (commit `4e2f34e`) shows good instinct: fix at the call site to preserve a tested contract, make the zone config-driven, degrade gracefully on bad zone names.
- **Net-of-fee paper PnL** with buy-side fee approximation is documented and correct enough for its purpose.
- **PLAN.md §7 is honest** — it self-identified the drawdown stub and the real-venue realized-PnL gap (both confirmed here). That self-awareness is rare; keep it.

---

## Suggested Priority Order

1. **H1** price refresh in paper mode (unblocks honest daily-loss rule, unrealized PnL, dashboard data, and M5 exits — highest leverage single fix).
2. **M5** deterministic SL/TP exit check (safety + PnL realism; depends on H1).
3. **H2** implement drawdown via persisted high-water mark (PLAN already recommends option b) and move position-size validation *into* the gate.
4. **H3** fix `enabled: false` semantics (small, but a trust-destroying surprise).
5. **M1** persist/rehydrate paper portfolio + risk state; add `execution.initial_cash`.
6. **M2/M3** decision-history quality: attribute outcomes to entry decisions, exclude fallback rows from context.
7. **H4** real-CCXT integration pass for the keyed path (balance parsing, spot positions strategy, limit-order fill tracking) before offering testnet mode at all.
8. **M4 + doc mismatches** dependency cleanup (remove pandas-ta/python-dotenv or use them; declare dev extras; add `.env.example`; reconcile AGENTS.md testing claims).
9. **M6–M9** stocks guard weekends, data depth, storage retention, agent/runner dedup.
