## Project Facts

Autonomous paper-trading agents powered by a local LLM. The goal: safe, testable trading agents for crypto and stocks before any live deployment. Single user, in Slovakia (EEA — MiCA rules apply). Details behind every rule below: `ARCHITECTURE.md`; the story behind each §7.N: `HISTORY.md`.

### Stack

- Python 3.11+, async-first (`asyncio`); Pydantic models (data **and** config); SQLite via SQLAlchemy + aiosqlite (WAL); APScheduler; httpx; structlog
- Crypto: CCXT → **OKX Europe** (ccxt `myokx`, `eea.okx.com`), EUR-quoted pairs only (USDT not tradable for EEA). Public data needs no key; keyed runs use the OKX **demo** (`testnet: true` → ccxt sandbox) or ack-gated live (§7.41)
- Stocks: yfinance data (`data/stocks_provider.py`); execution on **Saxo OpenAPI** (SIM first, §7.66). The XTB path is dead (API closed 2025-03-14) and kept disabled until the Saxo SIM run
- Config: `config/settings.yaml` (all tunables, commented) + `.env` for secrets
- Docs: `README.md` (overview), `ARCHITECTURE.md` (module map, data flow, schema, design), `PLAN.md` (the single list of open work — todos, gaps, open questions, limitations, risks), `HISTORY.md` (done work), `CHANGE.md` (multi-strategy design), `docs/API_NOTES.md` (venue/data APIs), `docs/reviews/` (external reviews; `[R4-xx]` → `external_4.md`)

### Commands

```bash
pytest              # all tests + coverage; `-m network` live smokes are deselected by default
pytest -m network   # opt-in live provider smokes (needs internet)
ruff check .        # lint
ruff format .       # format (100 chars, LF)
```

CI (`.github/workflows/ci.yml`) runs exactly these on **Python 3.11** with `pip install -e ".[dev,stocks]"` — keep all three green before committing. `sqlalchemy[asyncio]` is required (greenlet).

### Testing

- Mock every external API; no real network calls outside `network`-marked smokes. `pytest-asyncio` auto mode.
- Shared scaffolding in `tests/helpers.py`: `make_settings(tmp_path, overrides)` (deep-merged minimal config — never hand-write a settings.yaml), `StubAgent`, `runner_patches` + `run_agent_once`.
- Unit tests for every pure function (>90 % on `core/`), integration tests for the pipeline/agents, `hypothesis` properties for the risk engine.

### Architecture

```
agents/      thin per-market agents on agents/base_agent.py
core/        runner, decision_pipeline, risk_engine, llm_client, storage/, config, context,
             summarizer, sleeves, watchlist, backtester, control plane, timeutil
data/        ccxt + stocks providers; data/context/ market-context providers
execution/   paper (default), ccxt spot, Saxo, XTB (dead); shared FIFO position_tracker
analysis/    indicators, screener, baselines, prompt_builder, context_cards, sanitize
dashboard/   FastAPI + Jinja2/HTMX; writes latches, never orders
monitoring/  structlog + alerts
```

**Decision pipeline:** fetch → mark positions → exit levels / time stop → closed-bar indicators → read book once → market context → prompt (market data + YOUR BOOK + MARKET CONTEXT + honest track record) → LLM → `TradeSignal` → risk gate (+ backstop, + event guard for BUYs) → persist decision → execute → store. All executors implement the `Executor` Protocol (`place_order`, `get_positions`, `cancel_order`, `get_cash`, `close`) and expose a `venue` string.

Key models (`core/models.py`): `TradeSignal`, `DecisionRecord`, `RiskResult`, `Position` / `PortfolioState`, `MarketSnapshot`, `OrderResult`, `SymbolContext` / `ContextCard`.

### Coding Rules

- Type hints everywhere; async for I/O, sync for pure computation.
- Pydantic for data structures **and settings blocks** (`core/config.py`: `extra="forbid"`, a YAML `null` = the default unless the field takes `None`, project rules as validators with actionable messages).
- Protocols for swappable components; structlog only (no `print`); config-driven — never hardcode thresholds or endpoints.
- Datetimes: `core/timeutil.py` (`to_utc`, `to_naive_utc`) — SQLite stores naive UTC; never hand-roll `replace(tzinfo=UTC)`.
- **LF line endings only**, every file (`.gitattributes` + ruff enforce it).
- **No backward-compat shims:** renames are clean breaks (single user) — no old-name fallbacks, aliases or tolerated obsolete keys.

### Safety Rules

**Execution & money**
- Paper executor is the default; never assume live trading. Nothing executes without the risk gate; risk rules are deterministic code, never LLM judgement.
- Real money needs `testnet: false` + `crypto_agent.live_trading: true` + env `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK` (same ack for Saxo `environment: live`, XTB `account_type: real`). Never suggest `testnet: false` alone. Venue switches are never on the dashboard's safe-config surface.
- Secrets live in `.env`/environment only — never YAML, logs, errors or pages. `LLM_API_KEY`, `EXCHANGE_API_KEY/_SECRET/_PASSPHRASE`, `SAXO_ACCESS_TOKEN`, `SAXO_APP_KEY/_SECRET`, `ALERT_WEBHOOK_URL`. Saxo OAuth tokens: `data/saxo_<env>.token.json`, 0600, gitignored.
- `enabled: false` means nothing runs — runners exit before constructing any component; a single cycle is only ever the explicit `--once`.
- One runner per agent × mode (`flock` on `<data_dir>/<mode>_<agent>.runner.lock`; exit 2 if held, exit 3 on `--mode` mismatch, §7.52/§7.78).

**Risk engine**
- Seven entry rules (§7.5/§7.42/§7.54): confidence, max positions, per-**position** size cap (existing exposure + BUY), daily loss (rolls the UTC day at the check, §7.59), drawdown vs a seeded peak (escape only via `scripts/rebaseline_drawdown.py`, never a button), loss-streak cooldown, stop required with sane geometry.
- **Exits are never gated** (§7.47): a SELL closes the whole held long; only confidence applies. SL/TP (§7.9), time stops (§7.71) and close-all bypass the LLM and the gate. Close with `closing_side(position)` and check levels with `exit_level_breach` — never hardcode `OrderSide.SELL` (§7.48).
- Sizing (`calculate_quantity`) runs before the gate and the approved plan is what executes; it is cost-aware and never rounds up (§7.59, §7.65). No usable price → no LLM call, no order (§7.55).
- Event guard (§7.18): no BUY around high-impact macro events, earnings or after a delisting notice; reads calendar rows only, never LLM text; an unreadable context blocks entries.

**State & storage**
- One SQLite file per agent × mode (`<data_dir>/<mode>_<agent>.db`), mode derived from the executor's venue, identity-checked (`DatabaseIdentityError`) — a real run can never touch a paper file (§7.78).
- Every book/decision/order read is agent-scoped via `StorageBase._where_agent` / `_agent_scope` (§7.39) and venue-scoped — every row is venue-stamped and reads match exactly (§7.61/§7.76). Sleeve tables go through `SleeveMixin._scoped`. Bind the venue **before** seeding risk trackers.
- Restart safety (§7.7/§7.25/§7.58/§7.77): paper book, FIFO lots with entry decision ids, venue ledgers (net of reported fees), exit levels, pending orders, daily baseline and loss streak (from closing fills only) all rehydrate from SQLite.
- `portfolio_snapshots` and `sleeve_snapshots` are never pruned; `real_*` books never prune decisions/orders. No column migrations are carried — a schema change ships its own one-off migration.
- Post-order persistence is fail-soft and lossless (§7.44): never let an exception escape after `place_order` succeeded; reconciled venue statuses are confirmed only after they are stored.

**LLM**
- Keep `llm.max_response_chars ≥ 4 × max_tokens` (pinned by a test); `max_tokens` is the completion cap, not the context window; `timeout_seconds` must cover a whole non-streamed completion.
- The parser strips reasoning blocks and takes the last valid JSON object (§7.57). Fallback HOLDs are audit-only (`is_fallback`, never forgeable, never re-fed) and raise an alert (§7.51).
- Raw news text never reaches the trading prompt — only validated, bounded `ContextCard`s (§7.18); every external string in a prompt goes through `analysis/sanitize.py::safe_label`. The summarizer shares one lock with the trading client.

**Venues**
- `CcxtExecutor` is spot-only: positions come from its FIFO ledger capped by `fetch_balance`; `fetch_positions` is never used. Cash = `fetch_free_balance()` of the quote currency — **no currency argument** (real ccxt signature). Every pair must be quoted in `quote_currency` (startup and overrides).
- The pipeline prices, the venue executor executes (§7.75): BUY = limit at `close × (1 + entry_offset_pct)` (reserved in sizing via `buy_price_factor` — never name it `slippage_pct`), SELL = market; working orders age out, are never stacked, and dust is written off.
- Saxo (§7.66): long-only whole shares, one account currency, instruments via `symbol_map` or an unambiguous lookup (never guessed), fills from the audit log. XTB (§7.40): close via `type=CLOSE`, never flip into a short.

**Control plane & dashboard**
- The DB is the control source of truth (`agent_control`, row key = runner component `crypto`/`stocks`). Close-all runs even while paused; a broken control read never halts trading.
- Safe-config overrides (`SafeConfigOverrides`, `extra="forbid"`): risk values may only tighten; applied as YAML baseline + override every time (removal reverts, §7.50).
- Dashboard: routes by book key `<mode>_<agent>` (`?book=`), never places orders; every write needs the CSRF token (`_require_csrf`); Host allowlist + origin checks (§7.43). The launcher only stops processes it started or adopted (§7.24).

**Opt-in features (all off in code defaults)** — with the flag off, behaviour is exactly as without the feature:
- Watchlist (§7.70/§7.83): deterministic screener, capped + TTL'd, venue-tradable symbols only; core and held symbols are never dropped.
- Sleeves (§7.71–§7.73): one pipeline per sleeve, symbol lock, time stops, per-sleeve books and risk engines, agent-wide backstop; ownership derived from the ledger. Never shift weights by hand before the allocator (§7.74).
- Market context (§7.18): shipped **on** for both agents (stocks agent itself off); the summarizer ships off (CHANGE.md Q7). Keep `macro_calendar.events` extended (a warning fires when it runs out).

### Documentation Rules

- After every change, update `AGENTS.md` (rules only — keep it short), `README.md` and `PLAN.md`; architecture changes also go into `ARCHITECTURE.md`.
- When a PLAN §7 item completes, move its write-up to `HISTORY.md` under its original number and remove it from PLAN §7. §7.N identifiers are never renumbered or reused.
- Every todo, gap, open question or accepted limitation lives **only** in `PLAN.md` (next free §7.N, a severity, a row in *Order of work*) — never as a to-do list in another doc.
