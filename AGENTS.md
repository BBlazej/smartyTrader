## Project Facts

This is an autonomous paper-trading agent system powered by a local LLM (LM Studio). The goal is to build safe, testable trading agents for crypto and stocks before any live deployment.

### Stack

- Python 3.11+, async-first (`asyncio`)
- Data models: Pydantic `BaseModel`
- DB: SQLite via SQLAlchemy + aiosqlite (WAL mode — one writer, concurrent readers for the dashboard/backtester)
- Scheduling: APScheduler
- HTTP client: httpx (for LLM calls)
- Crypto data/orders: CCXT → Kraken testnet
- Stocks data: yfinance, xAPI (XTB demo)
- Structured logging: structlog
- Config: YAML (`config/settings.yaml`) + `.env` for secrets
- Documentation map: `AGENTS.md` (this file — agent-facing facts/rules), `README.md` (overview & quickstart), `ARCHITECTURE.md` (architecture: modules, data flow, schema, control plane, design decisions), `PLAN.md` (gaps/todos/next steps only — §7 lives there), `HISTORY.md` (delivered work + completed §7 items write-ups), `nightly_finds.md` (bugs/gaps found during development), `review.MD` / `review2.md` (external reviews)
- Documentation: More specific topics are documented in `*.md` files in `docs/`
- Documentation: Code specifics are documented in comments and docstrings

### Commands

```bash
pytest              # Run all tests (unit + integration), with coverage on src/
ruff check .        # Lint
ruff format .       # Format (100 char line limit)
```

Tests use `pytest-asyncio` in auto mode. Mock external APIs — no real network calls in unit tests.

### Testing Strategy

- **Unit tests** — Every pure function/method; mock all external deps; target >90% coverage on `core/`
- **Integration tests** — Full decision pipeline with mocked provider + paper executor; agent lifecycle
- **Property-based tests** — Risk engine invariants via `hypothesis` (`tests/unit/test_risk_engine_properties.py`); storage consistency checks
- Dev dependencies live in `[project.optional-dependencies].dev` (`pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `ruff`) — install with `pip install -e ".[dev]"`; stocks data extra: `pip install -e ".[stocks]"` (yfinance)

### Architecture

The project follows a layered architecture (detailed diagrams & contracts: `ARCHITECTURE.md`):

```
agents/          ← Per-market agents (crypto, stocks) — thin subclasses of base_agent
core/            ← Shared infrastructure (LLM client, risk engine, storage, scheduler, runner factory)
data/            ← Market data providers (ccxt, xtb, yfinance)
execution/       ← Order placement adapters (kraken, xtb, paper)
analysis/        ← Feature engineering + prompt building
monitoring/      ← Logging + alerts
```

**Decision pipeline**: fetch data → mark open positions at the snapshot's last close (optional executor `update_price` hook — paper mode) → compute indicators → build prompt → call LLM → parse `TradeSignal` → risk check (`RiskResult`) → execute if approved → store.

All executors implement the same `Executor` Protocol: `place_order`, `get_positions`, `cancel_order`, `get_cash`, `close`.

### Key Models (in `src/core/models.py`)

- `TradeSignal` — LLM output (action, confidence, reasoning, stop_loss, take_profit)
- `DecisionRecord` — A prior decision + its realized PnL, fed back into the prompt ("learn from its own track record")
- `RiskResult` — Deterministic risk verdict (approved / rejected + reason)
- `Position` / `PortfolioState` — Portfolio tracking with PnL
- `MarketSnapshot` — OHLCV candles + computed indicators per symbol
- `OrderResult` — Order placement outcome

### Coding Rules

- **Type hints required** on all functions and class attributes
- **async/await for I/O**, synchronous for pure computation (indicators, risk rules)
- **Pydantic models** for all data structures — validate at boundaries
- **Protocol-based interfaces** for swappable components (executors, providers)
- **Structured logging** via structlog — no `print()` statements
- **Config-driven behavior** — read from `config/settings.yaml`, never hardcode thresholds or endpoints

### Safety Rules

- **`enabled: false` means nothing runs:** both runners check `<agent>.enabled` right after loading config and exit *before constructing any component* — no cycles, LLM calls, order placement or DB writes. Single-cycle runs are the explicit `--once` CLI flag, never a side effect of disabling an agent.
- **One lifecycle implementation (§7.13):** the enabled-gate, wiring, rehydration/pruning startup passes, `--once` and scheduled loops live in `core/runner.py::run_agent`; scripts pass only market-specific `build_components`/`build_agent` callbacks. Agent behavior (cycle loop, persistence, alerts) lives in `agents/base_agent.py::BaseTradingAgent`; market quirks hook via `_skip_cycle_reason()`.
- Paper executor (`paper_executor.py`) is the default. Never assume live trading.
- Risk engine runs before every order. Nothing executes without approval.
- API keys live in `.env` — never commit them, never log them.
- **Risk rules are hard-coded deterministic guards**, not LLM decisions.
- **Paper state + risk trackers rehydrate from SQLite at startup** (`core/rehydration.py`, called by both runners before the first cycle): cash/positions come from the latest portfolio snapshot via the `load_portfolio_state` hook, the daily-loss baseline from today's earliest snapshot, and the losing-streak/cooldown from closed decision outcomes. `execution.initial_cash` only seeds a fresh (empty) portfolio.
- **All seven risk rules are live:** the max-drawdown gate tracks a peak-equity high-water mark (`seed_peak_equity` ← `MAX(portfolio_snapshots.total_value)` at runner startup, so it survives restarts), and `evaluate(signal, portfolio, planned_notional=...)` caps the *proposed* order notional at `max_position_pct × total_value` — sizing is computed before the gate and reused unchanged at execution.
- **Paper positions are marked to market every cycle:** `DecisionPipeline._mark_positions` feeds each snapshot's last close into the executor's optional `update_price(symbol, close)` hook (`PaperExecutor`) *before* the risk check, so unrealized PnL, portfolio snapshots and the daily-loss rule reflect actual market moves. Real-venue executors report live prices and skip the hook.
- **Paper mode runs on live public data:** `scripts/run_crypto_agent.py` always fetches real Kraken OHLCV via CCXT (public endpoints need no API key, no sandbox mode), so even paper mode stores real snapshots/decisions. Execution is what stays simulated — no `KRAKEN_API_KEY` → `PaperExecutor`; key set → `KrakenExecutor` on a separate sandboxed, keyed client.
- **Outcome attribution is FIFO and reaches the entry decision:** every executor feeds its fills through `execution/position_tracker.py`, so closing fills carry `realized_pnl` plus `closed_entries` (per-entry-decision PnL). The pipeline persists each decision *itself* right after the risk gate (`PipelineResult.decision_id`) and passes that id into `place_order`; agents backfill entry decisions via `Storage.add_realized_pnl`. Venue fills are tracked gross of commission (create_order payloads report none); a sell with nothing locally tracked reports no outcome rather than a fake one.
- **LLM-fallback HOLDs are audit-only:** `TradeSignal.is_fallback` (stripped from model output, never forgeable) is stored in `llm_decisions.is_fallback`, excluded from `get_recent_decisions`, and full prompt+response is logged as an `llm_exchange` structlog event.
- **Exit levels are enforced deterministically:** `Position` carries the entry signal's `stop_loss`/`take_profit` (passed via `place_order`), and `DecisionPipeline._check_exit_levels` closes the position the moment a cycle's mark breaches them — no LLM call, no risk gate (exits only reduce exposure; cooldown/daily-loss blocks must not strand a position). Toggled by `risk.enforce_exit_levels`; these are local checks, not venue-side stop orders. The stop rule now requires stops on *entries* only.
- **Market-hours guard is timezone-aware:** the stocks guard compares the *local* wall clock to the `market_hours` window, localized via the config-driven `stocks_agent.market_timezone` (default `Europe/Warsaw`) so a UTC host stays correct. The zone is a setting, never hardcoded.
- **Market-hours guard is weekend/holiday aware (§7.10):** Saturdays/Sundays are always closed when a real window is configured; `stocks_agent.market_holidays` (ISO dates, validated at startup) covers exchange holidays; overnight windows (`"22:00-08:00"`) wrap across midnight instead of silently never running. Skips log the reason (`weekend` / `holiday` / `outside trading window`).
- **Stocks data depth & NaN integrity (§7.11):** daily stock candles are fetched over a `"6mo"` window so MACD (≥26 closes) exists in the prompt, and any candle row with a NaN OHLC cell is dropped rather than zero-filled — fake zero-lows would poison ATR/Bollinger readings the LLM sees.
- **Storage retention (§7.12):** both runners prune expired rows at startup and on `storage.prune_interval_minutes` via `core/retention.py` (fail-soft); defaults prune market snapshots older than `snapshot_retention_days` (30) while decisions/orders are kept unless `history_retention_days > 0`. `portfolio_snapshots` are never pruned — they seed the drawdown high-water mark. `scripts/prune_storage.py` runs the same policy out-of-band.
- **Control plane (§7.15 P1/P2):** the DB is the control source of truth — `agent_control` table (state/close-all latch/health/safe-config overrides, one row per agent). `BaseTradingAgent.run_cycle` reads it every cycle: overrides → close-all (executes even while paused; `DecisionPipeline.close_all_positions` bypasses LLM *and* risk gate — closes only reduce exposure) → pause skip, then stamps a heartbeat; the read is fail-soft and checks are strict (`is True`), so a broken control plane never halts trading. The agent-side FastAPI control API (`core/control_api.py`, off unless `control_api.enabled`, loopback-bound) writes latches + serves status/decisions/portfolio/config; **credentials are structurally absent** from every endpoint, and `PUT /api/config` accepts only the `SafeConfigOverrides` whitelist (`extra="forbid"`; applied to live objects via `core/control_config.parse_and_apply` — `interval_minutes` applies at restart).
- **Decision-replay backtesting (§7.14):** `core/backtester.py::DecisionReplayBacktester` re-simulates stored `llm_decisions` against fresh historical candles (`fetch_history` on both providers, paginated) through the *same* `RiskEngine` + `PaperExecutor` fee/slippage model — deterministic, zero LLM calls. Exit-level and sizing rules are shared functions (`exit_level_breach`/`calculate_quantity` in `decision_pipeline.py`) so replay can't drift from live; the risk engine's daily-loss/cooldown trackers see one wall-clock "today" during replay (documented limitation). CLI: `scripts/backtest.py`.

### Documentation Rules

- after every change, update `AGENTS.md`, `README.md` and `PLAN.md` with the latest state
- architecture changes (modules, data flow, schema, control plane, design decisions) → update `ARCHITECTURE.md`
- when a PLAN §7 item completes → move its write-up to `HISTORY.md` under its original §7.N number and leave a stub in `PLAN.md` (§7.N identifiers are never renumbered)
- new bugs/gaps discovered while developing → log them in `nightly_finds.md` (numbered findings)
