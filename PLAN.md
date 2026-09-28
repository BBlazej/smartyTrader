# Autonomous Trading Agent — Plan (Gaps, Todos & Next Steps)

## Introduction

This document tracks **what remains to be done**: open gaps, todos and next steps (canonical list: §7 below), plus the still-open Phase 4 iteration work and the risk register. What has been delivered lives in [HISTORY.md](HISTORY.md); how the system is built lives in [ARCHITECTURE.md](ARCHITECTURE.md).

**Document map**

| File | Purpose |
|---|---|
| [README.md](README.md) | project overview & quickstart |
| [ARCHITECTURE.md](ARCHITECTURE.md) | architecture: modules, data flow, schema, control plane, design decisions |
| [HISTORY.md](HISTORY.md) | delivered work: status snapshot, original Phase 1–2 plans, completed §7 items |
| **PLAN.md** (this file) | gaps, todos & next steps (§7), Phase 4 iteration, risks |
| `AGENTS.md` | agent-facing facts & rules for coding agents |
| [CHANGE.md](CHANGE.md) | design proposal under discussion: multi-strategy sleeves, allocator, research layer (news/screener) |
| `review.MD` / `review2.md` / `external_review3.md` / `external_4.md` | external full-codebase reviews (`[R-xx]` tags reference these; `[R4-xx]` = `external_4.md`) |

**Numbering rule:** §7.N identifiers (§7.1–§7.79) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-28):** 1086 tests passing at ~94% coverage, zero pytest warnings — on the local venv and on a clean Python 3.11 install running the CI job's steps (the GitHub workflow itself first runs on the next push). §7.1–§7.38 are complete except §7.18 (optional enrichment), §7.28 (keyed Kraken run — premise corrected by §7.41: Kraken spot has no sandbox) and §7.34 (venue-side OCO) (see [HISTORY.md](HISTORY.md)). External review 4 (`external_4.md`, 2026-09-24) opened §7.39–§7.60. Done so far: §7.39 (per-agent storage scoping), §7.40 (XTB closes via type=CLOSE, never flips), §7.41 (no accidental live Kraken trading; honest spot valuation), §7.42 (per-position cap), §7.43 (browser-safe dashboard/control API, tighten-only risk overrides), §7.44 (fail-soft, lossless post-order persistence), §7.45 (book-aware prompt), §7.46 (loss streak counted once per closing fill), §7.47 (exits never gated; SELL closes in full), §7.48 (side-aware closes and exit levels), §7.49 (backtester look-ahead removed), §7.50 (safe-config overrides actually apply — live interval reschedule, baseline+override re-apply, removal reverts, diff-only persistence), §7.51 (LLM outages surfaced; webhook alert channel), §7.52 (single-instance runner flock), §7.53 (audited CLI drawdown re-baseline), §7.54 (entry SL/TP geometry + optional risk-per-trade sizing), §7.55 (no price → no LLM call, no order), §7.56 (configurable timeframe, one decision per closed bar), §7.57 (reasoning-model tolerant LLM parser), §7.58 (venue executors rehydrate ledgers, exit levels and pending orders) §7.59 (review-4 low-severity bundle: cost-aware sizing, midnight rollover, sub-$1 indicators, compose, XTB symbol map, doc/DB housekeeping), §7.60 (CI on Python 3.11 — which caught a missing `greenlet` dependency, find #20), §7.61 (venue-tagged rows; partial fills recorded) §7.62 (XTB fills booked at the venue's price), §7.64 (crypto on OKX Europe — spot-only ccxt executor, EUR pairs, passphrase; Kraken removed), §7.65 (per-venue cost model: fee %/minimum commission/FX profiles for paper + backtester; OKX base-currency buy fees booked net) , §7.69 (per-decision LLM latency/token recording, dashboard p50/p95, `scripts/benchmark_llm.py` — the live benchmark pass awaits LM Studio) §7.79 (equity counts unbooked BUYs), §7.77 (restart replay net of fees), §7.76 (risk seeds venue-scoped), §7.75 (venue order lifecycle — marketable pricing, order TTL, no stacked exits, dust write-off, venue fill times, fees in outcomes), §7.70 (deterministic crypto screener + capped TTL watchlist manager, CHANGE.md P4 pulled forward — opt-in, held symbols never dropped) and §7.71 (crypto strategy sleeves, CHANGE.md P1 — opt-in: per-sleeve pipelines/playbooks, symbol lock, time stops, per-sleeve books + risk engines, agent-wide backstop, dashboard sleeve table). §7.66 step 1 (correcting the XTB docs — API closed 2025-03-14) also landed. All four critical findings are closed; no venue execution path can reach real money without `live_trading` + `LIVE_TRADING_ACK`. The open items reflect all open work consolidated from `review.MD`, `review2.md`, `external_review3.md`, `external_4.md`, and `nightly_finds.md`, sorted by severity.

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

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LLM gives bad signals | Financial loss (even paper) | Risk engine gates everything; conservative defaults |
| LLM is too slow | Missed trading opportunities | Timeout with HOLD fallback; optimize prompt size |
| Exchange API rate limits | Data gaps, failed orders | Rate limiting built in; exponential backoff |
| XTB API access delayed | Stocks agent blocked | Start with crypto only; use yfinance for data even without execution |
| Overfitting to paper trading | Live performance differs | Simulate fees/slippage; start small if going live |
| Hallucinated indicators | Wrong decisions | Validate LLM output against computed values; include raw numbers in prompt |
| LLM non-determinism | Backtest replay won't reproduce stored decisions | `temperature=0.2` set (not 0); optional determinism `seed` shipped (§7.33, model permitting); full prompt+response audit logging in place — decision-replay backtests (§7.14) need no LLM at all |
| Paper PnL is optimistic | Overstates strategy quality | Verify `PaperExecutor` fee/slippage defaults before trusting paper PnL against the §4.3 live-readiness gates |

---

## 7. Gaps & Next Steps

Updated after the full-codebase reviews of **2026-09-15** (`review.MD`), **2026-09-17** (`review2.md`), **2026-09-21** (`external_review3.md`), and **2026-09-24** (`external_4.md` — §7.39–§7.60). Bugs and gaps found during development are logged directly here with a severity and a place in the order of work (`nightly_finds.md` was retired 2026-09-25; its "find #N" labels survive in HISTORY/git history). Overlaps have been consolidated and all open items are grouped by severity below.

> **This section lists only open work.** Items §7.1–§7.27, §7.29–§7.33, §7.35, §7.37–§7.65, §7.67–§7.73, §7.75–§7.77, §7.79 were completed in 2026-09; their full write-ups live in [HISTORY.md](HISTORY.md) under their original numbers. §7.N identifiers are **never renumbered or reused**.

### Critical / high severity (open)

> Order of work (from `external_4.md` §7; §7.39–§7.58 done — the §4.3 paper clock can start, replay numbers are look-ahead-free and the venue prerequisites §7.40/§7.41/§7.48/§7.58 are in). §7.61 (venue history hygiene, finds #18/#19) also landed ahead of the keyed venue run (§7.28). §7.75 (venue order lifecycle) and §7.76 (venue-scoped risk seeds), both found by the demo round trips, were fixed the same day. §7.77 (restart replay net of fees) and §7.79 (unbooked BUYs counted in equity) followed. **Open (2026-09-28): §7.78 (high, one DB per agent × mode)** — before the §7.28 multi-day demo run.



78. **One database per agent × trading mode (paper / demo / real)** ⏳ [decided 2026-09-28 after §7.76; follows §7.77, before the §7.28 multi-day demo run] — **high** (removes the cross-venue bug class)
    - **Why:** today both agents and every account share one SQLite file. They are kept apart only by `agent`/`venue` filters that every query must remember (§7.39, §7.61, sleeve `_scoped`, §7.76). §7.76 was one missed filter, and it silently blocked every demo BUY. With a file per agent × mode, foreign rows are absent rather than filtered. It also gives the real-money file its own backup/retention policy and removes crypto↔stocks write contention.
    - **Decided (2026-09-28):** (1) paper and demo of the same agent **may run side by side**; (2) the §7.28 smoke-test rows (decisions 143–146 + their orders/snapshots) are **dropped** in the split, not migrated.
    - **Design:**
      - **The path is derived, never configured.** The mode comes from the executor's venue tag: `paper` → `paper`; `*-sandbox`, `saxo-sim`, `xtb-demo` → `demo`; `*-live`/`*-LIVE`, `saxo-live`, `xtb-real` → `real`. The file is `<storage dir>/<mode>_<agent>.db` (e.g. `data/demo_crypto.db`, `data/real_crypto.db`). `storage.database_path` becomes a directory (or its parent is used), and files appear only once a mode is used.
      - **Identity guard.** Each file records `(agent, mode)` in a `db_identity` meta table at creation. `Storage` refuses to open a file whose identity differs from the runner's (a real run can never write into a paper file, or the other way round) or a legacy file without identity (unless migrating).
      - **Runner order flips.** `build_components()` runs first, the venue decides the file, then storage opens and the risk seeds and rehydration run as today. The single-instance lock becomes `<mode>_<agent>.runner.lock`, so paper + demo crypto can run concurrently while two demo crypto runners still cannot.
      - **Kept as a second layer:** the `agent`/`venue` columns and their scoping stay (cheap; `real` may later hold more than one venue, e.g. two exchanges).
    - **Consumers:**
      - **Dashboard:** discovers `data/*_*.db`, adds an agent × mode picker, and routes Pause/Resume/Close-all/safe-config writes to that file's `agent_control`. The launcher's Start/Stop is per agent × mode (pidfile + log per pair).
      - **Control API:** per runner, unchanged in spirit; the port must be unique per concurrent runner (agent × mode).
      - **Scripts:** `backtest`, `prune_storage` and `rebaseline_drawdown` take `--agent` + `--mode` instead of a path. `demo_round_trip`/`demo_agent_round_trip` open only `demo_<agent>.db` (structurally unable to touch `real_*`).
      - **Docker:** same `agent-data` volume, several files; compose services pass nothing new (the mode follows the keys/config).
    - **Real-money file policy:** `real_*.db` is never pruned (`history_retention_days` ignored). It is backed up more often (its own `backup_keep`), and every smoke/test script refuses it.
    - **Migration (one-time `scripts/split_database.py`):** it backs up `data/trading_agent.db`, then splits by agent and venue. Legacy NULL-venue and `paper` rows go to `paper_<agent>.db`; `*-sandbox`/`saxo-sim`/`xtb-demo` rows go to `demo_<agent>.db`; any live rows go to `real_<agent>.db`. Rows without a venue column follow their decision/order links (decisions → the venue of their orders, else paper). Per-agent `agent_control`, allocations, sleeve snapshots, watchlist entries and resets are copied into the matching file(s). The §7.28 smoke-test rows (reasoning `SMOKE TEST…`, decisions 143–146, their orders 1–2 and the snapshots those runs wrote) are dropped. It prints the row counts per file and leaves the original untouched as the backup.
    - **Tests:** path derivation for every venue label, the identity guard (mismatch and legacy refusals), concurrent paper + demo locks, dashboard discovery/routing, the split script on a fixture DB (incl. the smoke-row drop), and scripts refusing `real_*`.
    - **Docs:** AGENTS/ARCHITECTURE/README drop "one shared SQLite file" and describe the per-mode layout and identity guard.

### Medium severity (open)

> Order of work (2026-09-26, venues decided — **OKX Europe** for crypto, **Saxo** for stocks, CHANGE.md Q1; **crypto first**, ≈ €1,000 budget, free services only): ~~§7.64~~ (done) → ~~§7.65~~ (done) → §7.28 (OKX demo smoke run — keys work, overnight run clean, round trips done, §7.75 fixed and paper fees aligned 2026-09-28; the agent-driven round trips found §7.76 (fixed) and §7.77 — §7.77 fixed and re-verified; next: split the DB per agent × mode (§7.78), then the multi-day run in a clean `demo_crypto.db`) → ~~crypto screener + watchlist~~ (done as §7.70) → ~~§7.71~~ (crypto strategy sleeves, CHANGE.md P1 — done) → §7.66 (Saxo executor, SIM/paper only; per-symbol exchange windows land with it) → ~~§7.73~~ (performance ledger + baselines, CHANGE.md P2 — done) → §7.74 (allocator, CHANGE.md P3 — once the two sleeves have paper history). These are the prerequisites of the CHANGE.md proposal (§5) and make paper numbers honest for the §4.3 gates.

66. **Stocks executor → Saxo OpenAPI (replaces the dead XTB path)** ⏳ [found 2026-09-26; broker decided 2026-09-26]
    - XTB disabled API access on 2025-03-14 ("XTB no longer offers API access"); our client targets `wss://ws.xapi.pro`, known only from third-party wrappers, and its module docs wrongly claim trading "now lives" there. Saxo (Danish bank, serves Slovakia) has a free developer **SIM** environment ($100k, no funding) and the same OpenAPI for live after app approval.
    - Scope (decided 2026-09-26): **US and EU-listed stocks**; **paper/SIM only** until the budget grows (≈ €1,000 now; the $1 minimum is ~1 %/side at €100 positions); market data from **Saxo if free and adequate**, else yfinance — compare both here.
    - Fix, in order: (1) ✅ done 2026-09-26 — XTB docs/comments corrected everywhere (module docstrings, config, README/ARCHITECTURE/API_NOTES/AGENTS: API closed 2025-03-14, `ws.xapi.pro` labelled an unofficial third-party relay, path kept disabled); (2) ✅ done 2026-09-27 — `execution/saxo_client.py::SaxoClient` (httpx; SIM/LIVE gateways, bearer token from env `SAXO_ACCESS_TOKEN`, `ErrorInfo` errors, token never logged) + `execution/saxo_executor.py::SaxoExecutor` behind the unchanged `Executor` protocol: account selection (explicit key or unique account in `account_currency`), Uic lookup via `saxo_execution.symbol_map` or an unambiguous search, whole-share long-only market orders, one-currency rule (FX-quoted instruments refused — US stocks from a USD account), fills from the order-activity audit log with the §7.62 sanity check, ledger positions capped by net positions, per-cycle two-phase reconciliation, `load_fills`/`load_pending_orders` hooks, venue `saxo-sim`/`saxo-live` (live behind `LIVE_TRADING_ACK`); the stocks runner prefers it when enabled (config error if XTB is enabled too). Protocol checked against the developer portal + `saxo_openapi` docs; tests are mocked (`tests/unit/test_saxo_executor.py`).
    - **Next (needs the user's Saxo developer account):** (3) first SIM run — `--once` with a 24 h token, confirm account/instrument resolution, a buy + sell round trip, audit-log fill prices and net-position capping; compare Saxo SIM prices with yfinance (CHANGE.md Q8). (4) OAuth app (authorization code + refresh token) so unattended runs survive the 24 h token. (5) Only after a successful SIM run: switch the shipped stocks config to Saxo and delete the XTB executor/client (+ their tests/config) — kept until then as the known-dead reference.

74. **Deterministic allocator (CHANGE.md P3)** ⏳ [CHANGE.md §4.3, logged 2026-09-27 after §7.73]
    - Weekly job (config) that re-weights sleeves for *new entries only* (never force-closes): score each sleeve on its trailing window (Sharpe-like on sleeve equity, net of fees; 0 unless it beats its best baseline — §7.73 math), shrink toward equal weights by sample size (`w = n/(n+k)·w_perf + k/(n+k)·w_equal`, k ≈ 30 closed trades), clamp to `[min_weight, max_weight]`, cap the change per rebalance (±10 pp), renormalize, and write an audited `strategy_allocations` row (inputs, scores, old → new; reason `rebalance`). A sleeve latched by its drawdown guard scores 0 until re-baselined.
    - The job fetches history for the baselines (the dashboard cannot — it is DB-only), so it also persists each sleeve's baseline scores; the dashboard sleeve table then gains vs-baseline columns from those rows.
    - Operator pin (safe-config override, tighten-only spirit of §7.43) and `min_weight`/`max_weight` per sleeve in `sleeves.strategies.*.budget`.
    - Needs weeks of two-sleeve paper history before its numbers mean anything (CHANGE.md P1/P2 "done when"); replay over stored history first.

28. **Keyed venue smoke pass** ⏳ [R1-H4, §7.6 follow-up, find #1] — *re-scoped by §7.41*
    - Kraken **spot has no sandbox**, so the original "Kraken testnet" run cannot exist. **Now concrete (2026-09-26):** run it on the **OKX Europe demo** — §7.64 landed, the shipped config is ready (`exchange: myokx`, `testnet: true`; put the demo API key + secret + passphrase in `.env` as `EXCHANGE_API_KEY`/`_SECRET`/`_PASSPHRASE`) — first `--once`, then a multi-day paper run; confirms order format, fills, reconciliation and fee reporting against a real API. Needs a network-enabled environment.
    - **First keyed demo run (2026-09-27):** OKX EEA demo keys work (read-only check: sandbox mode on `eea.okx.com`, demo balance 4,600 EUR + pre-loaded BTC/ETH — never agent positions, the ledger only knows the agent's own fills). The first `--once` cycle placed nothing but found two real bugs, both fixed: (a) `CcxtExecutor.get_cash` called `fetch_free_balance("EUR")`, but real ccxt's signature is `(params={})` — every keyed cycle died with "'str' object is not a mapping" (the stub accepted the wrong call; a test now runs the real ccxt method); (b) a stale Kraken-era dashboard override (`pairs: BTC/USDT, ETH/USDT`, `interval_minutes: 1`) replaced the EUR pairs — `pairs` overrides now obey the startup quote-currency rule (skipped at apply time, rejected at write time). Next: clear the stale override, re-run `--once`, then a multi-day demo run.
    - **Second dry-run pass (2026-09-28):** two more finds fixed — (c) real ccxt `fetch_tickers()` returns a `{symbol: ticker}` dict, not a list (`CCXTProvider.fetch_quote_volumes` now handles both shapes; pinned by a test); (d) the EEA **demo** account lists only ~29 EUR spot pairs vs ~243 live, so the screener could add symbols the keyed executor cannot trade — `CcxtExecutor.tradable_symbols()` now whitelists candidates inside `WatchlistManager` (CHANGE.md P4's "only venue-tradable symbols"; paper stays unlimited). Re-run `--once` (watchlist enabled) to confirm end-to-end, then the multi-day demo run.
    - **Overnight demo run (2026-09-27 23:03 → 09-28 06:33 UTC):** clean — 5-min cycles without gaps, 0 errors/fallbacks, screener added NEAR/EUR + SOL/EUR (both demo-tradable), 34 decisions = 34 HOLDs (LLM 13–26 s/call, prompts 0.9k→2.7k tokens as history filled, completions ≤ 1k) through a −1.4…−5.2 % drift, so **no order was placed** and the venue path stayed unexercised. No log file was captured (the runner ran in a terminal) — redirect output or start it from the dashboard next time.
    - **Controlled round trip (2026-09-28, `scripts/demo_round_trip.py`):** forces one ~€20 BUY → SELL through the keyed `CcxtExecutor` (demo-only, runner lock held, no DB writes). Pipeline pricing (limit = live last close 73,111.8 vs demo ask 73,125.0) **rested 60 s and was cancelled**; priced 0.2 % across the close (a since-removed `--cross-pct` flag — the executor now does this itself, §7.75) both legs filled (BUY avg 73,096.0, SELL avg 73,087.4). Confirmed working: OKX `create_order` returns only an id (`status: None` → recorded `pending`) and `reconcile_open_orders`/`confirm_reconciled` resolved both within one poll; the BUY booked **net** of the base-currency fee (ledger 0.0002730029 = venue BTC delta exactly, §7.65); the SELL closed the ledger with a realized outcome. Finds → §7.75 (fixed the same day, see HISTORY).
    - **Round trip after §7.75 (2026-09-28):** the BUY limit (+0.2 %) filled at 73,126.5 and the SELL filled at market, both resolved in the placement call with venue fill times. The dust was written off and the net realized PnL matched the cash change to the dust. **Fees resolved (2026-09-28):** the account reports **maker 0.10 % / taker 0.20 %** and every agent order is a taker, so the crypto paper profile now charges 0.20 %/side (`execution.paper_costs.crypto.paper_fee_pct: 0.002`) — paper, backtest replay and the §7.73 baselines use it. Re-check with the round-trip report when keys change (a live account may sit on another tier). **Next:** one pipeline-driven round trip, then the multi-day demo run.
    - **Agent-driven round trip (2026-09-28, `scripts/demo_agent_round_trip.py`):** the real runner ran twice (scripted BUY; restart; scripted SELL) against the real DB. The BUY was **rejected by the drawdown gate** because of a peak inherited from old paper history, so no order was placed and the restart/SELL leg had nothing to close. → **§7.76** (critical). Rows written: decisions 143/144 (rejected, reasoning marked `SMOKE TEST`), portfolio snapshots, heartbeat. DB backed up first (`data/backups/trading_agent-pre-agent-roundtrip-20260928.db`).
    - **Agent-driven round trip after §7.76 (2026-09-28):** both runs traded. Run 1: the BUY decision (145) was approved, sized at 10 % of the 4,600 EUR book (0.00630517 BTC ≈ 460 EUR, filled at 72,986.18 in the placement call) and persisted. The order row is venue-tagged `myokx-sandbox`, linked to its decision, with the venue's fill time. The snapshot shows the net position with SL/TP. Run 2: a fresh runner replayed 1 fill (1 open symbol); the SELL decision (146) filled at 72,948.42. `realized_pnl` landed on the order row, on the SELL decision and via `closed_entries` on the entry decision 145. The final snapshot is flat and the heartbeat carries no error. **Find → §7.77:** after the restart the replayed ledger was gross of the BTC buy fee, so the SELL sold 1.26e-5 BTC of pre-loaded demo coins, and the PnL shows −1.16 EUR instead of ≈ −2.08 EUR.
    - **Agent round trip after §7.77 (2026-09-28):** OKX demo status polls timed out (`50004`), so the BUY (decision 147, 0.00631492 BTC @ 72,876.27) stayed `pending`. The first restart's SELL (148) was correctly rejected: nothing known to be held. Once the API recovered, `--sell-only` re-tracked the pending row at startup and reconciled it, persisting its `fee_base` 1.263e-5 BTC. The ledger was replayed **net** (0.00630229016 BTC), and the SELL (149) sold exactly that (`fee_quote` 0.918 EUR). Realized **−2.0404 EUR matches the cash change exactly** (4,598.5801 → 4,596.5397) and is backfilled onto entry decision 147. Find → §7.79 (equity dips while a fill is unconfirmed; fixed the same day).
    - *Done meanwhile:* per-cycle status reconciliation of orders left `open` is implemented and pinned — `KrakenExecutor.reconcile_open_orders()` re-polls pending venue orders each cycle (agent-side `_reconcile_orders`), patches the stored row via `Storage.update_order_status`, and flows late fills through the FIFO ledger with entry-decision attribution (§7.28).

### Low severity / housekeeping (open)

18. **Data-enrichment feeds** ⏳ (optional)
    - Sentiment provider (crypto) and economic-calendar feed (stocks) are aspirational context enrichments.
    - Expanded into a full proposal (news/events ingest, context cards, screener, watchlist) in [CHANGE.md](CHANGE.md) §4.4.

34. **Venue-side stop orders (OCO)** ⏳ [§7.9 follow-up]
    - SL/TP enforcement is local to the agent; venue-side OCO stop orders (OKX demo algo orders, later Saxo) remain future work — worth revisiting together with the §7.28 keyed run.



