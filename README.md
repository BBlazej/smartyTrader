# Autonomous Trading Agent

Paper-trading agents powered by a local LLM (any OpenAI-compatible server, e.g. LM Studio).
Two agents — **crypto** (OKX Europe) and **stocks** (yfinance data; Saxo OpenAPI SIM
execution opt-in, PLAN §7.66 — the XTB path is dead) — share one decision pipeline, one
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

pytest                      # 1285 tests, no network needed (live smokes are opt-in:
                            # `pytest -m network`, §7.63)
python -m scripts.run_crypto_agent   # run the crypto agent (paper by default)
python -m scripts.run_stocks_agent   # run the stocks agent (paper by default)
python -m scripts.run_crypto_agent --once   # exactly one cycle, then exit
python -m scripts.run_crypto_agent --mode demo --profile test   # test profile: trades often (paper/demo only)
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
src/agents/      per-market agents (crypto, stocks) on a shared base agent
src/core/        runner, decision pipeline, risk engine, LLM client, storage, config,
                 market context, sleeves, watchlist, backtester, control plane
src/data/        market data (ccxt, yfinance) + data/context/ market-context providers
src/execution/   paper, ccxt spot (OKX), Saxo OpenAPI, XTB (dead) executors + FIFO ledger
src/analysis/    indicators, screener, baselines, prompt + context-card building
src/dashboard/   FastAPI + Jinja2/HTMX web UI
src/monitoring/  structlog setup + alerts
scripts/         runners, dashboard, backtest, prune, re-baseline, login/benchmark tools
config/          settings.yaml (all tunables)        tests/  unit + integration
```

Module-by-module map: [ARCHITECTURE.md → Module layout](ARCHITECTURE.md#module-layout-current).

## Configuration

Everything is driven by `config/settings.yaml` + `.env` — no hard-coded
thresholds. Key sections:

| Section | What it controls |
|---|---|
| `llm` | LM Studio endpoint, model, `timeout_seconds` (whole non-streamed completion; 300), retries + `retry_backoff_base_seconds` (exponential backoff), JSON-schema opt-in, `temperature`, `max_tokens` (completion cap, 8192 — *not* the context window, which is set in LM Studio), `max_response_chars` (size guard, keep ≈ 4 × `max_tokens`) |
| `crypto_agent` | enabled, exchange, testnet flag, `live_trading` (§7.41 live-money opt-in, default false), interval, pairs, `decision_history_limit`, `timeframe` (default `1h`), `decide_on_new_bar_only` (one LLM decision per closed bar; cycles in between only mark + enforce exits — §7.56), `watchlist` (§7.70: opt-in deterministic screener adding up to `max_dynamic_symbols` extra pairs with a TTL — liquidity floor → volatility band → momentum rank; core pairs + held symbols never dropped; optional `news_mentions` priority for candidates named in recent news, §7.83), `sleeves` (§7.71: opt-in strategy sleeves — per-sleeve `timeframe`, `playbook` (`swing`/`position`) and `holding` time stop over the same pairs; a symbol is held by one sleeve at a time; each sleeve trades `weight` × allocated capital with its own `risk:` limits, plus an agent-wide `backstop_max_drawdown_pct`), `context` (§7.18: market context — `sentiment`, `macro`, `announcements`, `earnings`, `news` feeds + aliases, `summarizer` with optional `llm` overrides; shipped on for crypto, summarizer off) |
| `stocks_agent` | enabled, broker, demo, interval, `market_hours` (wrap-around windows supported), `market_timezone` (zone the window is in), `market_holidays` (ISO closure dates; weekends always closed), symbols, `decision_history_limit`, `timeframe` (default `1d`), `decide_on_new_bar_only` (§7.56), `context` (§7.18 — yfinance earnings + EDGAR feeds), `exchanges` + `symbol_exchanges` (§7.66: per-exchange windows for a US + EU universe) |
| `risk` | max position %, daily loss limit, max drawdown, cooldown (`consecutive_losses_cooldown_minutes` + `consecutive_losses_threshold` streak), max positions, min confidence, `max_stop_distance_pct` + optional `risk_per_trade_pct` sizing (entry-level geometry, §7.54), `enforce_exit_levels` (deterministic SL/TP closes), event guard (§7.18): `event_guard_enabled`, `event_blackout_before/after_minutes`, `event_guard_min_importance`, `earnings_blackout_days_before`/`_hours_after`, `delisting_blackout_days` |
| `execution` | paper-executor fee %, slippage %, and `initial_cash` (seeds a fresh portfolio; persisted state wins after the first cycle) |
| `venue_orders` | Keyed crypto venue orders (§7.75): `entry_offset_pct` (BUY limit above the close, default 0.2 %), `exit_order_type` (`market` \| `limit`), `exit_offset_pct`, `order_ttl_seconds` (cancel still-working orders; 0 = never), `fill_confirm_delay_seconds`. Paper ignores it |
| `storage` | One SQLite file per agent × trading mode (§7.78): `data_dir/<mode>_<agent>.db`, the mode derived from the executor's venue and guarded by a `(agent, mode)` identity table; WAL mode — concurrent reads while the agent writes. Retention windows: `snapshot_retention_days` (default 30), `history_retention_days` (0 = keep forever; `real_*` books never prune), `prune_interval_minutes`, `context_retention_days` (market-context rows, §7.18) |
| `macro_calendar` | Scheduled macro events shared by both agents (§7.18): `events` (curated, UTC — FOMC + ECB decisions through 2027) and `feed_url` (ForexFactory weekly JSON; `""` disables) |
| `monitoring` | log level, alert dedup window, `alert_webhook_format` (`json` for Slack/Discord/generic, `ntfy`) + `alert_min_severity` — the webhook URL itself comes only from the `ALERT_WEBHOOK_URL` env var (§7.51) |
| `control_api` | agent-side control API: `enabled` (default false), `host` (loopback), per-agent ports (§7.15) |
| `dashboard` | web dashboard bind (`host`/`port`, loopback defaults), HTMX `refresh_seconds`, `agents` shown/controlled (§7.15 P3/P4) |
| `saxo_execution` | Saxo OpenAPI stocks execution (§7.66): `enabled` (default false → paper), `environment` (`sim`, or `live` + `LIVE_TRADING_ACK`), `account_key` / `account_currency` (one currency — US stocks from a USD account), `symbol_map` (data → Saxo symbol, e.g. `AAPL: "AAPL:xnas"`), `fill_poll_delays`, `amount_decimals` (0 = whole shares), `oauth` (§7.66: OAuth app instead of the 24 h token — `redirect_uri`, `token_file`, `keepalive_minutes`; login once with `python -m scripts.saxo_login`). At most one of `saxo_execution` / `xtb_execution` may be enabled |
| `xtb_execution` | XTB **demo** execution via xAPI: `enabled` (default false → paper), `host`, `account_type` (demo|real, validated at startup), `request_timeout_seconds`, `symbol_map` (data → xAPI symbols, e.g. `AAPL: AAPL.US`; §7.59 L8); requires env creds `XTB_ACCOUNT_ID`/`XTB_ACCOUNT_PASSWORD` (§7.16) |

### Environment variables

| Variable | Effect |
|---|---|
| `LOCAL_LLM_ENDPOINT` | Override the LLM endpoint — any OpenAI-compatible server (LM Studio, Unsloth desktop `http://localhost:8889/v1`, llama-server, Ollama) |
| `LLM_API_KEY` | Bearer key for LLM servers that require one (Unsloth desktop, llama-server `--api-key`); unset → no `Authorization` header (LM Studio) |
| `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` / `EXCHANGE_API_PASSPHRASE` | Keyed crypto execution (OKX needs all three): demo keys with `testnet: true`; live only with §7.41's ack; public data needs no key |
| `LIVE_TRADING_ACK` | Must be exactly `I_ACCEPT_REAL_MONEY_RISK` for any real-money path: a keyed exchange with `testnet: false` or `xtb_execution.account_type: real` (§7.41) |
| `LOCAL_LLM_USE_JSON_SCHEMA` | Opt-in strict JSON response mode |
| `SAXO_ACCESS_TOKEN` | Saxo OpenAPI bearer token (§7.66; SIM: the 24 h developer token) — used only when `saxo_execution.enabled`; env only, never logged |
| `SAXO_APP_KEY` / `SAXO_APP_SECRET` | Saxo OAuth app credentials (§7.66 step 4, `saxo_execution.oauth.enabled`) — `scripts/saxo_login.py` stores the token pair, the runner refreshes it |
| `XTB_ACCOUNT_ID` / `XTB_ACCOUNT_PASSWORD` | XTB **demo** execution (§7.16): account id + xAPI verification code from xStation; used only when `xtb_execution.enabled: true`, else paper stays |

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

Paper trading runs end to end on both markets: live public data (OKX Europe, yfinance),
a local LLM, seven deterministic risk rules plus the market-context event guard,
restart-safe books (one SQLite file per agent × mode), strategy sleeves and a screener
watchlist (both opt-in), decision-replay backtests with dumb baselines, and a web
dashboard. Keyed execution is verified on the OKX **demo**; Saxo SIM (stocks) is built
and awaits a developer account. Real money stays double-gated (below).
**1285 tests passing at ~95% coverage.** Delivered work: [HISTORY.md](HISTORY.md);
open work (the single list): [PLAN.md](PLAN.md).

> **Real money is double-gated (§7.41):** a keyed live exchange executor is only ever built with
> `crypto_agent.testnet: false` **and** `live_trading: true` **and**
> `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`; anything less stays on paper (or on the OKX demo with
> `testnet: true`) and logs why (the same ack gates Saxo `environment: live`).

## Documentation map

| File | Contents |
|---|---|
| `README.md` (this file) | overview & quickstart |
| `ARCHITECTURE.md` | architecture: module map, data flow, storage schema, control plane, design decisions |
| `AGENTS.md` | rules and facts for coding agents |
| `PLAN.md` | all open work: order of work, §7 items, live-readiness gate, backlog, limitations, risks |
| `HISTORY.md` | delivered work, completed §7 items |
| `CHANGE.md` | multi-strategy design: sleeves, allocator, research layer (P1/P2/P4/P5 done, P3 open) |
| `docs/API_NOTES.md` | venue and data-source API notes |
| `docs/reviews/` | external full-codebase reviews (`[R-xx]` / `[R4-xx]` tags) |
