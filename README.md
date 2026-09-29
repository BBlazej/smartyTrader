# Autonomous Trading Agent

Paper-trading agents powered by a local LLM (LM Studio). Two agents — **crypto**
(OKX Europe) and **stocks** (XTB demo — API closed, moving to Saxo, PLAN §7.66) — share one decision pipeline, one
deterministic risk engine, and one storage layer.

> **Safety-first:** the paper executor is the default and nothing executes
> without passing the risk gate. The LLM proposes; a hard-coded risk engine
> disposes. Live trading is never assumed.

## How it works

```
fetch data → compute indicators → build prompt (market data + own book + market context
        + track record) → call LLM → parse TradeSignal → risk check (RiskResult + event guard)
        → execute if approved → persist (decision / order / portfolio)
```

The LLM never bypasses the risk engine. If any rule is violated, the signal is
rejected with a reason and nothing is sent to the exchange.

### What makes it different

- **Learns from its own track record.** Each cycle feeds the LLM the agent's last
  N prior decisions — action, confidence, reasoning, risk verdict — *and the
  realized PnL once each position closed* (net of fees). The model sees what it
  decided **and how it turned out**, so it can avoid repeating losing patterns.
- **Realistic paper PnL.** The paper executor models per-side fees, slippage and
  per-venue cost schedules — the OKX account's own taker rate (0.20 %) for crypto, Saxo's 0.08 % with a
  minimum commission plus FX fee for stocks (§7.65) — so realized PnL (and the
  win/loss the risk engine tracks) is net-of-fee and comparable to the "win rate >
  50% after fees" live-readiness gate.
- **Knows what is on the calendar (§7.18).** A background job pulls free, key-less
  context — the crypto Fear & Greed index, scheduled macro events (a curated FOMC/ECB
  list plus the ForexFactory weekly feed), OKX delisting notices, RSS news — into the
  DB. The prompt gets a sanitized MARKET CONTEXT section, and a deterministic **event
  guard** blocks new entries around high-impact events and after a delisting notice
  (exits are never gated). An optional LLM summarizer turns news into strict,
  bounded context cards; raw news text never reaches the trading prompt.
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

pytest                      # 1254 tests, no network needed (live smokes are opt-in:
                            # `pytest -m network`, §7.63)
python -m scripts.run_crypto_agent   # run the crypto agent (paper by default)
python -m scripts.run_stocks_agent   # run the stocks agent (paper by default)
python -m scripts.run_crypto_agent --once   # exactly one cycle, then exit
python -m scripts.backtest --days 30        # replay stored decisions vs fresh candles (§7.14)
python -m scripts.run_dashboard             # web dashboard at http://127.0.0.1:8080 (§7.15 P3/P4)

# or run the whole system in containers (§7.15 P5):
docker compose up -d --build                # crypto agent + dashboard (loopback 127.0.0.1:8080)
docker compose --profile stocks up -d        # + stocks agent (set stocks_agent.enabled: true first)
docker compose run --rm backtester --days 30  # on-demand replay (tools profile)
```

A disabled agent (`crypto_agent.enabled: false` / `stocks_agent.enabled: false`)
exits immediately **without running anything** — no cycles, LLM calls, order
placement or DB writes. Single-cycle mode is the explicit `--once` flag, never
a side effect of disabling an agent.

**Paper mode is the default.** The crypto agent always runs on **live public
market data** — OKX Europe's public OHLCV endpoint (ccxt `myokx`, EUR pairs; §7.64)
needs no API key and no sandbox mode, so even the paper path generates real snapshots,
indicators, and LLM signals. The **first cycle runs immediately at startup**, then repeats
every `interval_minutes`. Execution stays simulated: without `EXCHANGE_API_KEY` in `.env`
(the runner loads it for you — no `python-dotenv` needed), orders are filled by the
fee/slippage-aware `PaperExecutor`. With OKX **demo-trading** keys
(`EXCHANGE_API_KEY`/`_SECRET`/`_PASSPHRASE`) and the default `testnet: true`, orders go to
the OKX demo (mode `myokx-sandbox`). Real money needs `crypto_agent.testnet: false`,
`crypto_agent.live_trading: true` **and** env `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`
(mode `<exchange>-LIVE`, §7.41); anything else stays on paper and logs why.
Keyed orders follow the `venue_orders` block (§7.75): entries are limits 0.2 % across the
last close (sizing reserves it), exits go at market, orders still working after
`order_ttl_seconds` are cancelled, and outcomes are net of the fees the venue reports.
To exercise the venue order path without waiting for the LLM to trade,
`python -m scripts.demo_round_trip` (dry run; `--yes` to place) forces one small BUY → SELL
round trip on the OKX **demo** through the same keyed executor. It is demo-only, holds the
runner lock and writes nothing to the DB (§7.28). Its report shows the order terms sent,
the venue's fee payloads and the account's fee tier. `python -m scripts.demo_agent_round_trip`
(`--yes` to run) does the same through the **real agent runner**: a scripted BUY, a restart,
a scripted SELL. The orders, decisions and snapshots land in the DB, marked `SMOKE TEST`.
The stocks runner
executes on the `PaperExecutor` by default. The old **XTB demo** path (§7.16) is
dead: XTB closed its API access on 2025-03-14, and `wss://ws.xapi.pro` (what our
client targets) is an unofficial third-party relay — keep `xtb_execution.enabled:
false`. A Saxo OpenAPI executor replaces it (PLAN §7.66).

## Project layout

```
src/
├── core/
│   ├── models.py             # Pydantic models (TradeSignal, DecisionRecord, Position, OrderResult, Executor protocol)
│   ├── config.py             # YAML + env settings loader
│   ├── llm_client.py         # LM Studio HTTP client (retry + think-tolerant JSON parse + HOLD fallback)
│   ├── risk_engine.py        # 7 deterministic risk rules (all live)
│   ├── storage/              # SQLite (SQLAlchemy + aiosqlite) repository package (§7.36)
│   ├── decision_pipeline.py  # fetch → indicators → prompt → LLM → risk → persist decision → execute
│   ├── rehydration.py        # Restores paper book, venue ledgers/exit levels/pending orders + risk trackers at startup
│   ├── retention.py          # Fail-soft storage pruning wrapper (startup + scheduled)
│   ├── runner.py             # Shared runner lifecycle: enabled-gate, wiring, --once/scheduled loops
│   ├── backtester.py         # Decision-replay backtester: same risk/fee model, zero LLM calls (§7.14)
│   ├── performance.py        # Per-sleeve performance ledger: trades, win rate, profit factor, holding time, max DD (§7.73)
│   ├── control_api.py        # Agent-side FastAPI control API (pause/resume/close-all/config) (§7.15)
│   ├── control_config.py     # Safe config-override whitelist (credentials structurally impossible) (§7.15)
│   ├── watchlist.py          # Capped TTL watchlist manager over the screener (§7.70)
│   ├── sleeves.py            # Strategy sleeves: ledger-derived ownership, symbol lock, time stops (§7.71)
│   ├── context.py            # Market context: fail-soft refresh job + per-decision reader (§7.18)
│   ├── summarizer.py         # Batch LLM news summarizer → validated context cards (§7.18)
│   └── scheduler.py          # APScheduler wrapper
├── data/
│   ├── ccxt_provider.py      # Crypto OHLCV via CCXT (OKX Europe)
│   ├── xtb_provider.py       # Stocks OHLCV (yfinance source; xAPI is the seam)
│   └── context/              # Market-context providers (§7.18): Fear & Greed, macro calendar
│                             #   (YAML + ForexFactory), OKX delisting notices, yfinance
│                             #   earnings, RSS/Atom news + EDGAR filings
├── execution/
│   ├── paper_executor.py     # Simulated executor (default; fee + slippage + net PnL)
│   ├── position_tracker.py   # Shared FIFO cost-basis ledger → realized PnL per entry decision
│   ├── ccxt_executor.py      # Keyed ccxt spot orders — OKX demo, live only with §7.41 ack
│   ├── saxo_executor.py      # Saxo OpenAPI stock orders — SIM first; ledger-capped long-only (§7.66)
│   ├── saxo_client.py        # Saxo OpenAPI REST client (accounts, instruments, orders, fill audit)
│   ├── xtb_executor.py       # XTB demo orders (DEAD path — API closed 2025-03-14; §7.66 → Saxo)
│   └── xtb_client.py         # Real xAPI WebSocket client (§7.16): unofficial ws.xapi.pro relay
├── agents/
│   ├── base_agent.py         # Shared cycle loop, post-process, persistence, alerts (§7.13)
│   ├── crypto_agent.py       # Thin subclass (24/7, no hours guard)
│   └── stocks_agent.py       # Thin subclass + market-hours guard (weekend/holiday/wrap)
├── analysis/                 # Feature engineering + prompt building (§7.17)
│   ├── indicators.py         # compute_indicators: RSI/MACD/Bollinger/ATR (pure, moved from core)
│   ├── screener.py           # Deterministic universe screening: liquidity/vol/momentum (§7.70)
│   ├── baselines.py          # Dumb baselines a strategy must beat: buy & hold, 20/50 MA crossover, cash (§7.73)
│   ├── context_cards.py      # Summarizer prompt + strict card parser (injection defenses, §7.18)
│   ├── sanitize.py           # safe_label: plain bounded text for external strings in prompts
│   └── prompt_builder.py     # build_user_prompt (+ MARKET CONTEXT section) + DEFAULT_SYSTEM_PROMPT
├── monitoring/
│   ├── logger.py             # structlog setup
│   └── alerts.py             # AlertManager + sinks (Noop)
└── dashboard/                # Web UI (§7.15 P3/P4): FastAPI + Jinja2/HTMX, reads the WAL DB
    ├── app.py                # Pages + HTMX control endpoints (latch writes; SafeConfigOverrides form)
    ├── views.py              # Pure view-models: win-rate/confidence stats, uPlot shaping, positions
    └── templates/            # base / overview / decisions / positions / context / config / _health

scripts/
├── run_crypto_agent.py       # Entry point — crypto-specific factories + shared runner
├── run_stocks_agent.py       # Entry point — stocks-specific factories + shared runner
├── run_dashboard.py          # Web dashboard server (monitor + control + safe config) (§7.15)
├── prune_storage.py          # Out-of-band retention pruning (no agents, no trades)
├── rebaseline_drawdown.py    # Audited CLI drawdown peak re-baseline — the latch's only exit (§7.53)
├── backtest.py               # Decision replay vs fresh historical candles + net baselines; --strategy replays one sleeve (§7.73)
└── benchmark_llm.py          # LLM decision-latency benchmark on real prompts — p50/p95 + watchlist sizing (§7.69)

config/settings.yaml          # All tunables (LLM, pairs, risk, execution, monitoring)
Dockerfile                    # Slim image (python:3.11, non-root) for all services (§7.15 P5)
docker-compose.yml            # agent-crypto/-stocks + dashboard + on-demand backtester (§7.15 P5)
.github/workflows/ci.yml      # lint + format check + pytest on Python 3.11 (§7.60)
tests/
├── unit/                     # Fast, no network
└── integration/              # Full pipeline, mocked provider, real SQLite
```

## Configuration

Everything is driven by `config/settings.yaml` + `.env` — no hard-coded
thresholds. Key sections:

| Section | What it controls |
|---|---|
| `llm` | LM Studio endpoint, model, `timeout_seconds` (whole non-streamed completion; 300), retries + `retry_backoff_base_seconds` (exponential backoff), JSON-schema opt-in, `temperature`, `max_tokens` (completion cap, 8192 — *not* the context window, which is set in LM Studio), `max_response_chars` (size guard, keep ≈ 4 × `max_tokens`) |
| `crypto_agent` | enabled, exchange, testnet flag, `live_trading` (§7.41 live-money opt-in, default false), interval, pairs, `decision_history_limit`, `timeframe` (default `1h`), `decide_on_new_bar_only` (one LLM decision per closed bar; cycles in between only mark + enforce exits — §7.56), `watchlist` (§7.70: opt-in deterministic screener adding up to `max_dynamic_symbols` extra pairs with a TTL — liquidity floor → volatility band → momentum rank; core pairs + held symbols never dropped), `sleeves` (§7.71: opt-in strategy sleeves — per-sleeve `timeframe`, `playbook` (`swing`/`position`) and `holding` time stop over the same pairs; a symbol is held by one sleeve at a time; each sleeve trades `weight` × allocated capital with its own `risk:` limits, plus an agent-wide `backstop_max_drawdown_pct`), `context` (§7.18: market context — `sentiment`, `macro`, `announcements`, `earnings`, `news` feeds + aliases, `summarizer` with optional `llm` overrides; shipped on for crypto, summarizer off) |
| `stocks_agent` | enabled, broker, demo, interval, `market_hours` (wrap-around windows supported), `market_timezone` (zone the window is in), `market_holidays` (ISO closure dates; weekends always closed), symbols, `decision_history_limit`, `timeframe` (default `1d`), `decide_on_new_bar_only` (§7.56), `context` (§7.18 — yfinance earnings + EDGAR feeds, prepared but off) |
| `risk` | max position %, daily loss limit, max drawdown, cooldown (`consecutive_losses_cooldown_minutes` + `consecutive_losses_threshold` streak), max positions, min confidence, `max_stop_distance_pct` + optional `risk_per_trade_pct` sizing (entry-level geometry, §7.54), `enforce_exit_levels` (deterministic SL/TP closes), event guard (§7.18): `event_guard_enabled`, `event_blackout_before/after_minutes`, `event_guard_min_importance`, `earnings_blackout_days_before`/`_hours_after`, `delisting_blackout_days` |
| `execution` | paper-executor fee %, slippage %, and `initial_cash` (seeds a fresh portfolio; persisted state wins after the first cycle) |
| `venue_orders` | Keyed crypto venue orders (§7.75): `entry_offset_pct` (BUY limit above the close, default 0.2 %), `exit_order_type` (`market` \| `limit`), `exit_offset_pct`, `order_ttl_seconds` (cancel still-working orders; 0 = never), `fill_confirm_delay_seconds`. Paper ignores it |
| `storage` | One SQLite file per agent × trading mode (§7.78): `data_dir/<mode>_<agent>.db`, the mode derived from the executor's venue and guarded by a `(agent, mode)` identity table; WAL mode — concurrent reads while the agent writes. Retention windows: `snapshot_retention_days` (default 30), `history_retention_days` (0 = keep forever; `real_*` books never prune), `prune_interval_minutes`, `context_retention_days` (market-context rows, §7.18) |
| `macro_calendar` | Scheduled macro events shared by both agents (§7.18): `events` (curated, UTC — FOMC + ECB decisions through 2027) and `feed_url` (ForexFactory weekly JSON; `""` disables) |
| `monitoring` | log level, alert dedup window, `alert_webhook_format` (`json` for Slack/Discord/generic, `ntfy`) + `alert_min_severity` — the webhook URL itself comes only from the `ALERT_WEBHOOK_URL` env var (§7.51) |
| `control_api` | agent-side control API: `enabled` (default false), `host` (loopback), per-agent ports (§7.15) |
| `dashboard` | web dashboard bind (`host`/`port`, loopback defaults), HTMX `refresh_seconds`, `agents` shown/controlled (§7.15 P3/P4) |
| `saxo_execution` | Saxo OpenAPI stocks execution (§7.66): `enabled` (default false → paper), `environment` (`sim`, or `live` + `LIVE_TRADING_ACK`), `account_key` / `account_currency` (one currency — US stocks from a USD account), `symbol_map` (data → Saxo symbol, e.g. `AAPL: "AAPL:xnas"`), `fill_poll_delays`, `amount_decimals` (0 = whole shares). At most one of `saxo_execution` / `xtb_execution` may be enabled |
| `xtb_execution` | XTB **demo** execution via xAPI: `enabled` (default false → paper), `host`, `account_type` (demo|real, validated at startup), `request_timeout_seconds`, `symbol_map` (data → xAPI symbols, e.g. `AAPL: AAPL.US`; §7.59 L8); requires env creds `XTB_ACCOUNT_ID`/`XTB_ACCOUNT_PASSWORD` (§7.16) |

### Environment variables

| Variable | Effect |
|---|---|
| `LOCAL_LLM_ENDPOINT` | Override the LLM endpoint (formerly `LM_STUDIO_ENDPOINT`, still read with a deprecation warning) — any OpenAI-compatible server (LM Studio, Unsloth desktop `http://localhost:8889/v1`, llama-server, Ollama) |
| `LLM_API_KEY` | Bearer key for LLM servers that require one (Unsloth desktop, llama-server `--api-key`); unset → no `Authorization` header (LM Studio) |
| `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` / `EXCHANGE_API_PASSPHRASE` | Keyed crypto execution (OKX needs all three): demo keys with `testnet: true`; live only with §7.41's ack; public data needs no key |
| `LIVE_TRADING_ACK` | Must be exactly `I_ACCEPT_REAL_MONEY_RISK` for any real-money path: a keyed exchange with `testnet: false` or `xtb_execution.account_type: real` (§7.41) |
| `LM_STUDIO_USE_JSON_SCHEMA` | Opt-in strict JSON response mode |
| `SAXO_ACCESS_TOKEN` | Saxo OpenAPI bearer token (§7.66; SIM: the 24 h developer token) — used only when `saxo_execution.enabled`; env only, never logged |
| `XTB_ACCOUNT_ID` / `XTB_ACCOUNT_PASSWORD` | XTB **demo** execution (§7.16): account id + xAPI verification code from xStation; used only when `xtb_execution.enabled: true`, else paper stays |
| `XTB_API_KEY` | *Deprecated* — the old placeholder for §7.16; no longer read by any code |

## Running & testing

```bash
pytest                          # all tests (unit + integration) with coverage
pytest tests/unit/ -k risk      # a subset
ruff check .                    # lint
ruff format .                   # format (100-char lines)
```

Tests mock all external dependencies — **no real network calls** in the suite.
CI (`.github/workflows/ci.yml`, §7.60) runs `ruff check`, `ruff format --check` and `pytest`
on Python 3.11 — the Docker image's version — for every push to `main` and every PR.

## Safety rules

- **Paper executor is the default.** Never assume live trading.
- **Risk engine runs before every order.** Nothing executes without approval.
- **API keys live in `.env`** — never committed, never logged (see `.gitignore`).
- **Risk rules are hard-coded, deterministic guards** — not LLM decisions.
- **`enabled: false` means nothing runs.** The runner exits before constructing
  any component; use `--once` when a single intentional cycle is wanted.

## Status

Phases 1–5 complete: shared core (LLM client, risk engine, storage, scheduler,
decision pipeline, with indicators & prompt in the extracted `analysis/` layer), crypto provider + executor +
agent, **stocks provider + executor + agent**, paper executor with
fee/slippage modeling, monitoring (structured logging), both
entry scripts, the **learn-from-your-own-track-record loop** (prior decisions
+ realized PnL fed back to the LLM), and the **crypto agent on real data**
(the paper path fetches live public OKX Europe OHLCV — no API key needed — while
execution stays simulated), the **timezone-aware market-hours guard** (the
stocks window is compared in the config-driven `market_timezone`, so a UTC host
stays correct), **SQLite WAL mode** (concurrent reads while the agent writes),
and **per-cycle position marking** (open paper positions are re-marked at each
snapshot's last close before the risk check, so unrealized PnL and the
daily-loss rule track the market), and **honest `enabled: false` semantics**
(both runners exit without running anything when an agent is disabled; `--once`
is the explicit single-cycle flag), a **single runner process per agent** (exclusive
file lock — a double-start refuses with exit code 2, §7.52), and the **drawdown guard is live** (peak
equity high-water mark persisted via SQLite, seeded at startup) with the
**order-size cap enforced at the gate** (oversized plans are rejected before
execution; sells clamp to units held), and the **keyed ccxt spot path hardened
against real ccxt payloads** (nested balances, fill price/time recording, spot-only
positions from the fill ledger — §7.64; §7.41 removed any accidental path to
real money — OKX demo smoke run still pending, §7.28),
and **restart-safe paper state** (cash/positions rehydrate from the latest
portfolio snapshot; ccxt/XTB executors rebuild their FIFO ledgers, entry SL/TP and pending
orders from stored rows, §7.58 — each executor only from its own venue-tagged rows, §7.61; daily-loss baseline and losing-streak/cooldown rebuild from
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
with win-rate/confidence stats, agent health (heartbeat-derived — stale agents show
`offline`, not the last latch value); HTMX pause/resume/close-all controls and a
safe-config editor (risk limits can only be tightened; overrides apply immediately —
including re-arming the cycle interval — are stored only as diffs against
`settings.yaml`, and removing one reverts the live value; §7.50) — all writing the same `agent_control` latches, behind
Host-allowlist, cross-origin and CSRF-token guards (§7.43); `scripts/run_dashboard.py`,
§7.15 P3/P4), packaged for containers (`docker compose up -d --build` — agents, dashboard
and an on-demand backtester on one shared volume of per-mode SQLite books (§7.78) — each
agent × mode keeps its own file, book, drawdown peak and history; §7.15 P5, §7.39), and an XTB demo
execution path over xAPI (`execution/xtb_client.py`, §7.16) — **dead since XTB closed
its API on 2025-03-14**, kept disabled as reference until the Saxo executor lands
(PLAN §7.66). Paper stays the default everywhere.
**Market context (§7.18, CHANGE.md P5):** sentiment, macro calendar, venue delisting
notices and RSS news feed a sanitized MARKET CONTEXT prompt section and a deterministic
entry event guard; an opt-in LLM summarizer writes validated context cards; the dashboard
has a read-only `/context` page.
**1254 tests passing at ~95% coverage.**

Open work: see `PLAN.md` §7 (Gaps & Next Steps)
for the full list — reordered after the full-codebase reviews; detailed findings live in `review.MD`, `review2.md`, `external_review3.md`, and `external_4.md` at the repo root.

> **Real money is double-gated (§7.41):** a keyed live exchange executor is only ever built with
> `crypto_agent.testnet: false` **and** `live_trading: true` **and**
> `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`; anything less stays on paper (or on the OKX demo with
> `testnet: true`) and logs why (same ack for `xtb_execution.account_type: real`). All four critical findings of external review 4 are closed.

## Documentation map

| File | Contents |
|---|---|
| `README.md` (this file) | overview & quickstart |
| `ARCHITECTURE.md` | architecture: components, data flow, storage schema, control plane, design decisions (Mermaid diagrams) |
| `HISTORY.md` | delivered work: status snapshot, original Phase 1–2 plans, completed §7 items |
| `PLAN.md` | gaps, todos & next steps (§7), Phase 4 iteration, risk register |
| `CHANGE.md` | multi-strategy design: sleeves, capital allocator, research layer (P1/P2/P4/P5 implemented; P3 allocator open) |
| `AGENTS.md` | agent-facing facts & rules for coding agents |
| `review.MD` / `review2.md` / `external_review3.md` / `external_4.md` | external full-codebase architecture & code reviews |
