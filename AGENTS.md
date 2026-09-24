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
- Documentation map: `AGENTS.md` (this file — agent-facing facts/rules), `README.md` (overview & quickstart), `ARCHITECTURE.md` (architecture: modules, data flow, schema, control plane, design decisions), `PLAN.md` (gaps/todos/next steps only — §7 lives there), `HISTORY.md` (delivered work + completed §7 items write-ups), `nightly_finds.md` (bugs/gaps found during development), `review.MD` / `review2.md` / `external_review3.md` / `external_4.md` (external reviews; `[R4-xx]` tags → `external_4.md`)
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
dashboard/       ← Web UI (FastAPI + Jinja2/HTMX): monitor + control via the agent_control latches (§7.15 P3/P4)
```

**Decision pipeline**: fetch data → mark open positions at the snapshot's last close (optional executor `update_price` hook — paper mode) → compute indicators → read the book once → build prompt (market data + **YOUR BOOK**: this symbol's position/cash/limit headroom, §7.45 + last-N decisions with honest outcomes — `n/a` for HOLD/rejected/unfilled, `still open` only for executed entries) → call LLM → parse `TradeSignal` → risk check (`RiskResult`) → execute if approved → store.

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

- **Known open gaps (external review 4 → PLAN §7.39–§7.60) — several rules below state intent the code does not yet meet; do not rely on them until the item lands:**
  - **Kraken spot has no sandbox (§7.41, critical):** the keyed path crashes with `testnet: true` and trades **real funds** with `testnet: false` (still logged as "testnet"); spot valuation/exit enforcement/close-all do not work there. Never suggest `testnet: false` as a workaround.
  - **Also open:** `market_hours`/`interval_minutes` overrides never apply (§7.50 — contrary to "applies at restart" below); no single-instance runner lock (§7.52); venue executors (Kraken/XTB) do **not** rehydrate FIFO ledgers, exit levels or pending orders — §7.25 covers paper only (§7.58).
- **Code-nit baseline (§7.19):** `PipelineStep` is a real `StrEnum`; the LLM client retries with exponential backoff (`llm.retry_backoff_base_seconds`, 0 disables) and resolves its chat-completions URL once from any endpoint shape (no `/v1` string surgery); the cooldown streak is config-driven (`risk.consecutive_losses_threshold`, default 3 — also honored at restart rehydration, §7.26) alongside `llm.temperature`/`max_tokens`, an optional determinism `llm.seed` (null omits it) and a raw-response size guard `llm.max_response_chars` that fails oversized completions into the retry/fallback path instead of the parser (§7.33); `risk_engine.py` logs via structlog — safety-critical rejections never bypass the configured renderers.
- **`enabled: false` means nothing runs:** both runners check `<agent>.enabled` right after loading config and exit *before constructing any component* — no cycles, LLM calls, order placement or DB writes. Single-cycle runs are the explicit `--once` CLI flag, never a side effect of disabling an agent.
- **One lifecycle implementation (§7.13):** the enabled-gate, wiring, rehydration/pruning startup passes, `--once` and scheduled loops live in `core/runner.py::run_agent`; scripts pass only market-specific `build_components`/`build_agent` callbacks. Agent behavior (cycle loop, persistence, alerts) lives in `agents/base_agent.py::BaseTradingAgent`; market quirks hook via `_skip_cycle_reason()`.
- Paper executor (`paper_executor.py`) is the default. Never assume live trading.
- Risk engine runs before every order. Nothing executes without approval.
- API keys live in `.env` — never commit them, never log them.
- **Risk rules are hard-coded deterministic guards**, not LLM decisions.
- **Paper state + risk trackers rehydrate from SQLite at startup** (`core/rehydration.py`, called by both runners before the first cycle): cash/positions come from the latest portfolio snapshot via the `load_portfolio_state` hook — which also replays historical filled orders into the FIFO `PositionTracker` so open lots keep their cost basis **and entry decision ids** across a restart (§7.25; gaps left by pruned order history become synthetic lots at `avg_entry_price`) — the daily-loss baseline from today's earliest snapshot, and the losing-streak/cooldown from **closing fills** (`orders.realized_pnl` via `get_recent_closing_fills` — one outcome per closing fill, exactly what the live tracker counts; §7.46 — never from decision rows, which double-count round trips). Every close path (LLM sell, SL/TP exit, close-all, reconciled fill) calls `record_outcome`. `execution.initial_cash` only seeds a fresh (empty) portfolio.
- **All seven risk rules are live:** the max-drawdown gate tracks a peak-equity high-water mark (`seed_peak_equity` ← `MAX(portfolio_snapshots.total_value)` at runner startup, so it survives restarts), and `evaluate(signal, portfolio, planned_notional=...)` caps the *resulting position* — existing long exposure in the symbol (`long_exposure`) plus the proposed BUY notional — at `max_position_pct × total_value` (§7.42: per position, not per order; a BUY at the cap is rejected outright) — sizing (`calculate_quantity`, headroom = cap − existing) is computed before the gate and reused unchanged at execution.
- **Paper positions are marked to market every cycle:** `DecisionPipeline._mark_positions` feeds each snapshot's last close into the executor's optional `update_price(symbol, close)` hook (`PaperExecutor`) *before* the risk check, so unrealized PnL, portfolio snapshots and the daily-loss rule reflect actual market moves. Real-venue executors report live prices and skip the hook.
- **Paper mode runs on live public data:** `scripts/run_crypto_agent.py` always fetches real Kraken OHLCV via CCXT (public endpoints need no API key, no sandbox mode), so even paper mode stores real snapshots/decisions. Execution is what stays simulated — no `KRAKEN_API_KEY` → `PaperExecutor`; key set → `KrakenExecutor` on a separate sandboxed, keyed client.
- **Outcome attribution is FIFO and reaches the entry decision:** every executor feeds its fills through `execution/position_tracker.py`, so closing fills carry `realized_pnl` plus `closed_entries` (per-entry-decision PnL). The pipeline persists each decision *itself* right after the risk gate (`PipelineResult.decision_id`) and passes that id into `place_order`; agents backfill entry decisions via `Storage.add_realized_pnl`. Venue fills are tracked gross of commission (create_order payloads report none); a sell with nothing locally tracked reports no outcome rather than a fake one.
- **Venue orders left `open` are reconciled per cycle (§7.28):** `KrakenExecutor` remembers pending orders (with their entry decision + exit-level plan) and `BaseTradingAgent._reconcile_orders` polls them once per cycle through the optional `reconcile_open_orders()` executor hook; terminal statuses patch the stored `orders` row via `Storage.update_order_status` (which never blanks recorded fill data; a missing row is re-created), and late fills flow through the same FIFO ledger with entry-decision attribution. Fail-soft: a broken poll never halts a cycle. **Two-phase (§7.44):** a resolved status is re-delivered until the agent calls `confirm_reconciled(order_id)` after persisting it, so a DB error delays the record instead of losing it.
- **Post-order persistence is fail-soft and lossless (§7.44):** `_post_process` is contained per symbol (a storage error never aborts the cycle or skips the heartbeat); order rows are retried and, if still unwritable, dumped as an `order_persist_failed` audit log line + error alert. Never let a persistence exception escape after `place_order` succeeded. Paper orders fill instantly and XTB polls internally, so those executors have no hook.
- **LLM outages are loud (§7.51):** a fallback HOLD sets the cycle's `last_error` (dashboard health shows it, plus an "LLM fallbacks n/20" badge) and sends a deduplicated `llm_unavailable` alert. Alerts always go to the structlog log sink; `ALERT_WEBHOOK_URL` (env only — it is a secret) adds a `WebhookAlertSink` (`monitoring.alert_webhook_format: json|ntfy`, `alert_min_severity`).
- **LLM-fallback HOLDs are audit-only:** `TradeSignal.is_fallback` (stripped from model output, never forgeable) is stored in `llm_decisions.is_fallback`, excluded from `get_recent_decisions`, and full prompt+response is logged as an `llm_exchange` structlog event.
- **Exits are never gated (§7.47):** a SELL means *close the held long* (spot — never a short): the risk engine checks only confidence for it (daily loss, drawdown, cooldown, size and max-positions gate entries only), `calculate_quantity` sizes it to the whole position, and a SELL on a flat symbol is rejected at the gate.
- **Closes are side-aware (§7.48):** always close with `closing_side(position)` (SELL a long, BUY-cover a short) and check levels with `exit_level_breach` (short stops sit above entry) — never hardcode `OrderSide.SELL` for a close.
- **Exit levels are enforced deterministically:** `Position` carries the entry signal's `stop_loss`/`take_profit` (passed via `place_order`), and `DecisionPipeline._check_exit_levels` closes the position the moment a cycle's mark breaches them — no LLM call, no risk gate (exits only reduce exposure; cooldown/daily-loss blocks must not strand a position). Toggled by `risk.enforce_exit_levels`; these are local checks, not venue-side stop orders. The stop rule now requires stops on *entries* only.
- **Market-hours guard is timezone-aware:** the stocks guard compares the *local* wall clock to the `market_hours` window, localized via the config-driven `stocks_agent.market_timezone` (default `Europe/Warsaw`) so a UTC host stays correct. The zone is a setting, never hardcoded.
- **Market-hours guard is weekend/holiday aware (§7.10):** Saturdays/Sundays are always closed when a real window is configured; `stocks_agent.market_holidays` (ISO dates, validated at startup) covers exchange holidays; overnight windows (`"22:00-08:00"`) wrap across midnight instead of silently never running. Skips log the reason (`weekend` / `holiday` / `outside trading window`).
- **Stocks data depth & NaN integrity (§7.11):** daily stock candles are fetched over a `"6mo"` window so MACD (≥26 closes) exists in the prompt, and any candle row with a NaN OHLC cell is dropped rather than zero-filled — fake zero-lows would poison ATR/Bollinger readings the LLM sees.
- **Bar timing (§7.56):** each agent decides on `<agent>.timeframe` candles (crypto `1h`, stocks `1d`; never hardcode). Indicators use **closed bars only** (`analysis/candles.py::split_forming`); the forming bar is the live price for marking/exits and is labelled in the prompt. With `decide_on_new_bar_only: true` (default) the LLM is asked once per newly closed bar per symbol — cycles in between return `PipelineResult.skip_reason` after marking + exit enforcement; fallback HOLDs don't consume the bar; the last decision time survives restarts via storage.
- **Storage is agent-scoped (§7.39):** both agents share one SQLite file, so `llm_decisions`, `orders` and `portfolio_snapshots` carry an `agent` column. `run_agent` builds `Storage(path, agent=component)` — writes are stamped and every book/decision/order read filters on the binding (explicit `agent=` overrides; an unbound Storage — dashboard, CLIs — reads across agents). **Any new query on these tables must go through `_agent_scope`**, or one agent will rehydrate, seed its drawdown peak or build its prompt from the other's rows. Dashboard book pages take `?agent=`.
- **Storage retention (§7.12):** both runners prune expired rows at startup and on `storage.prune_interval_minutes` via `core/retention.py` (fail-soft), preceded by a timestamped online DB backup when `storage.backup_dir` is set (`backup_keep` rotates, §7.35); defaults prune market snapshots older than `snapshot_retention_days` (30) while decisions/orders are kept unless `history_retention_days > 0`. `portfolio_snapshots` are never pruned — they seed the drawdown high-water mark. `scripts/prune_storage.py` runs the same policy out-of-band.
- **Control plane (§7.15 P1/P2):** the DB is the control source of truth — `agent_control` table (state/close-all latch/health/safe-config overrides, one row per agent). The row key is the runner `component` name (`crypto`/`stocks`) — agent subclasses must pass exactly that as `BaseTradingAgent(component=...)`, or latches/heartbeats orphan silently (find #16, pinned by `tests/integration/test_control_loop.py`). `BaseTradingAgent.run_cycle` reads it every cycle: overrides → close-all (executes even while paused; `DecisionPipeline.close_all_positions` bypasses LLM *and* risk gate — closes only reduce exposure) → pause skip, then stamps a heartbeat; the read is fail-soft and checks are strict (`is True`), so a broken control plane never halts trading. The agent-side FastAPI control API (`core/control_api.py`, off unless `control_api.enabled`, loopback-bound) writes latches + serves status/decisions/portfolio/config; **credentials are structurally absent** from every endpoint, and `PUT /api/config` accepts only the `SafeConfigOverrides` whitelist (`extra="forbid"`; **risk values may only tighten** the YAML limits held in `Settings.risk_baseline` — checked at write *and* apply time, §7.43; `enforce_exit_levels` is not on the surface; applied to live objects via `core/control_config.parse_and_apply` — `interval_minutes` applies at restart).
- **Dashboard writes latches, never orders (§7.15 P3/P4):** `src/dashboard/` (`scripts/run_dashboard.py`, config block `dashboard:`) is a standalone FastAPI + Jinja2/HTMX app reading the shared SQLite DB (WAL). Pause/Resume/Close-all POST buttons and the safe-config form write the same `agent_control` rows as the control API — no HTTP coupling to the agent, no order placement. Config edits go through `validate_overrides_payload` → `SafeConfigOverrides` (`extra="forbid"`), so credential-shaped keys are rejected wholesale; credentials are structurally absent from every page. **Browser-safe (§7.43, `core/web_security.py`):** both web apps reject any request whose `Host` isn't allowlisted (loopback + bind host + `allowed_hosts` — DNS rebinding) and any write with a foreign `Origin`/`Referer`; every dashboard write also needs the per-process CSRF token its pages embed (`hx-headers` / hidden `csrf_token` field). New write endpoints must call `_require_csrf`. Health badges show an effective status derived from the heartbeat (`views.py::agent_status`: freshness checked FIRST — a stale beat reads `offline` even behind a `paused` latch); runners stamp the heartbeat on market-hours-skipped *and* paused cycles, so a stale beat unambiguously means the process is gone.
- **Start/stop agents from the dashboard (§7.24, opt-in):** `dashboard.allow_launch` (default false) wires `src/dashboard/launch.py::AgentLauncher` — health cards gain Start/Stop buttons; Start spawns `python -m scripts.run_<agent>_agent` locally (all enabled-gates/risk rules/paper defaults apply unchanged), logs to `data/agent_<name>.out.log`, pidfile `data/<agent>.pid`. Children outlive the dashboard; re-adoption requires a live pid whose `/proc` cmdline matches the runner. **Stop only ever kills processes this dashboard launched/adopted** — foreign agents can be paused via latch but never killed by pid guesswork. Start refuses (409) when heartbeat is fresh (no trading twins), disabled in config, or already managed. Keep `allow_launch: false` under docker-compose — services there belong to compose. Launched agents' output is viewable read-only at `/logs/{agent}` (tail of `data/agent_<name>.out.log`, HTMX-refreshed).
- **XTB orders reduce first, never flip (§7.40):** xAPI `SELL`+`OPEN` opens a short, so `XTBExecutor.place_order` closes opposite open trades FIFO with `XApiClient.close_trade` (`type=CLOSE` + the trade's `order` number) before anything else, drops any remainder, and refuses a SELL with nothing to close unless `allow_short=True`. Never route a closing order through `create_order`.
- **XTB demo execution is opt-in (§7.16):** the stocks runner wires `XTBExecutor` over the real xAPI client (`execution/xtb_client.py`) only when `xtb_execution.enabled` AND both `XTB_ACCOUNT_ID`/`XTB_ACCOUNT_PASSWORD` (the xStation xAPI verification code, not the login password) are set; anything missing keeps the paper executor and logs why. The block is deliberately **outside** the dashboard's safe-config whitelist — enabling real execution is never a web-form click. Protocol facts: hosts `wss://ws.xapi.pro/{demo,real}` (old ws.xtb.com retired 2025-03-14), classic WS `login` auth (**no OAuth2 endpoint exists**), instant orders + `tradeTransactionStatus` polling, positions marked via `getTickPrices`; fills tracked gross of commission, volume is xAPI lots (≈1 share per lot for XTB equities).
- **Docker packaging (§7.15 P5):** one slim image (`Dockerfile`, python:3.11-slim, non-root) + `docker-compose.yml` — `agent-crypto`, `agent-stocks`, `dashboard` (loopback-only host mapping `127.0.0.1:8080`) and on-demand `backtester` (`tools` profile). Shared named volume `agent-data` holds the SQLite WAL; `./config` is bind-mounted **read-only** (safe overrides live in `agent_control` DB rows — nothing writes config files). Secrets enter only via environment substitution (`${KRAKEN_API_KEY:-}` etc.; empty → paper executor) and `.dockerignore` guarantees `.env` is never baked into an image.
- **Replay has no look-ahead (§7.49):** candle events fire at bar *close* time (open + timeframe) — never price a replayed decision from a bar that had not closed yet.
- **Decision-replay backtesting (§7.14):** `core/backtester.py::DecisionReplayBacktester` re-simulates stored `llm_decisions` against fresh historical candles (`fetch_history` on both providers, paginated) through the *same* `RiskEngine` + `PaperExecutor` fee/slippage model — deterministic, zero LLM calls. Exit-level and sizing rules are shared functions (`exit_level_breach`/`calculate_quantity` in `decision_pipeline.py`) so replay can't drift from live; the risk engine's daily-loss/cooldown trackers see one wall-clock "today" during replay (documented limitation). CLI: `scripts/backtest.py`.

### Documentation Rules

- after every change, update `AGENTS.md`, `README.md` and `PLAN.md` with the latest state
- architecture changes (modules, data flow, schema, control plane, design decisions) → update `ARCHITECTURE.md`
- when a PLAN §7 item completes → move its write-up to `HISTORY.md` under its original §7.N number and **remove it from PLAN §7** — PLAN §7 lists only open work (§7.N identifiers are never renumbered or reused; HISTORY is the record)
- new bugs/gaps discovered while developing → log them in `nightly_finds.md` (numbered findings)
