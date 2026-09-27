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

**Numbering rule:** §7.N identifiers (§7.1–§7.73) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-27):** 985 tests passing at ~94% coverage, zero pytest warnings — on the local venv and on a clean Python 3.11 install running the CI job's steps (the GitHub workflow itself first runs on the next push). §7.1–§7.38 are complete except §7.18 (optional enrichment), §7.28 (keyed Kraken run — premise corrected by §7.41: Kraken spot has no sandbox) and §7.34 (venue-side OCO) (see [HISTORY.md](HISTORY.md)). External review 4 (`external_4.md`, 2026-09-24) opened §7.39–§7.60. Done so far: §7.39 (per-agent storage scoping), §7.40 (XTB closes via type=CLOSE, never flips), §7.41 (no accidental live Kraken trading; honest spot valuation), §7.42 (per-position cap), §7.43 (browser-safe dashboard/control API, tighten-only risk overrides), §7.44 (fail-soft, lossless post-order persistence), §7.45 (book-aware prompt), §7.46 (loss streak counted once per closing fill), §7.47 (exits never gated; SELL closes in full), §7.48 (side-aware closes and exit levels), §7.49 (backtester look-ahead removed), §7.50 (safe-config overrides actually apply — live interval reschedule, baseline+override re-apply, removal reverts, diff-only persistence), §7.51 (LLM outages surfaced; webhook alert channel), §7.52 (single-instance runner flock), §7.53 (audited CLI drawdown re-baseline), §7.54 (entry SL/TP geometry + optional risk-per-trade sizing), §7.55 (no price → no LLM call, no order), §7.56 (configurable timeframe, one decision per closed bar), §7.57 (reasoning-model tolerant LLM parser), §7.58 (venue executors rehydrate ledgers, exit levels and pending orders) §7.59 (review-4 low-severity bundle: cost-aware sizing, midnight rollover, sub-$1 indicators, compose, XTB symbol map, doc/DB housekeeping), §7.60 (CI on Python 3.11 — which caught a missing `greenlet` dependency, find #20), §7.61 (venue-tagged rows; partial fills recorded) §7.62 (XTB fills booked at the venue's price), §7.64 (crypto on OKX Europe — spot-only ccxt executor, EUR pairs, passphrase; Kraken removed), §7.65 (per-venue cost model: fee %/minimum commission/FX profiles for paper + backtester; OKX base-currency buy fees booked net) , §7.69 (per-decision LLM latency/token recording, dashboard p50/p95, `scripts/benchmark_llm.py` — the live benchmark pass awaits LM Studio) §7.70 (deterministic crypto screener + capped TTL watchlist manager, CHANGE.md P4 pulled forward — opt-in, held symbols never dropped) and §7.71 (crypto strategy sleeves, CHANGE.md P1 — opt-in: per-sleeve pipelines/playbooks, symbol lock, time stops, per-sleeve books + risk engines, agent-wide backstop, dashboard sleeve table). §7.66 step 1 (correcting the XTB docs — API closed 2025-03-14) also landed. All four critical findings are closed; no venue execution path can reach real money without `live_trading` + `LIVE_TRADING_ACK`. The open items reflect all open work consolidated from `review.MD`, `review2.md`, `external_review3.md`, `external_4.md`, and `nightly_finds.md`, sorted by severity.

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

> **This section lists only open work.** Items §7.1–§7.27, §7.29–§7.33, §7.35, §7.37–§7.65, §7.67–§7.71 were completed in 2026-09; their full write-ups live in [HISTORY.md](HISTORY.md) under their original numbers. §7.N identifiers are **never renumbered or reused**.

### Critical / high severity (open)

> Order of work (from `external_4.md` §7; §7.39–§7.58 done — the §4.3 paper clock can start, replay numbers are look-ahead-free and the venue prerequisites §7.40/§7.41/§7.48/§7.58 are in). Nothing critical/high remains open; §7.61 (venue history hygiene, finds #18/#19) also landed ahead of the keyed venue run (§7.28).

### Medium severity (open)

> Order of work (2026-09-26, venues decided — **OKX Europe** for crypto, **Saxo** for stocks, CHANGE.md Q1; **crypto first**, ≈ €1,000 budget, free services only): ~~§7.64~~ (done) → ~~§7.65~~ (done) → §7.28 (OKX demo smoke run — needs the user's demo keys + LM Studio for the §7.69 benchmark pass) → ~~crypto screener + watchlist~~ (done as §7.70) → ~~§7.71~~ (crypto strategy sleeves, CHANGE.md P1 — done) → §7.66 (Saxo executor, SIM/paper only; per-symbol exchange windows land with it) → §7.73 (performance ledger + baselines, CHANGE.md P2 — once the two sleeves have paper history). These are the prerequisites of the CHANGE.md proposal (§5) and make paper numbers honest for the §4.3 gates.

66. **Stocks executor → Saxo OpenAPI (replaces the dead XTB path)** ⏳ [found 2026-09-26; broker decided 2026-09-26]
    - XTB disabled API access on 2025-03-14 ("XTB no longer offers API access"); our client targets `wss://ws.xapi.pro`, known only from third-party wrappers, and its module docs wrongly claim trading "now lives" there. Saxo (Danish bank, serves Slovakia) has a free developer **SIM** environment ($100k, no funding) and the same OpenAPI for live after app approval.
    - Scope (decided 2026-09-26): **US and EU-listed stocks**; **paper/SIM only** until the budget grows (≈ €1,000 now; the $1 minimum is ~1 %/side at €100 positions); market data from **Saxo if free and adequate**, else yfinance — compare both here.
    - Fix, in order: (1) ✅ done 2026-09-26 — XTB docs/comments corrected everywhere (module docstrings, config, README/ARCHITECTURE/API_NOTES/AGENTS: API closed 2025-03-14, `ws.xapi.pro` labelled an unofficial third-party relay, path kept disabled); (2) ✅ done 2026-09-27 — `execution/saxo_client.py::SaxoClient` (httpx; SIM/LIVE gateways, bearer token from env `SAXO_ACCESS_TOKEN`, `ErrorInfo` errors, token never logged) + `execution/saxo_executor.py::SaxoExecutor` behind the unchanged `Executor` protocol: account selection (explicit key or unique account in `account_currency`), Uic lookup via `saxo_execution.symbol_map` or an unambiguous search, whole-share long-only market orders, one-currency rule (FX-quoted instruments refused — US stocks from a USD account), fills from the order-activity audit log with the §7.62 sanity check, ledger positions capped by net positions, per-cycle two-phase reconciliation, `load_fills`/`load_pending_orders` hooks, venue `saxo-sim`/`saxo-live` (live behind `LIVE_TRADING_ACK`); the stocks runner prefers it when enabled (config error if XTB is enabled too). Protocol checked against the developer portal + `saxo_openapi` docs; tests are mocked (`tests/unit/test_saxo_executor.py`).
    - **Next (needs the user's Saxo developer account):** (3) first SIM run — `--once` with a 24 h token, confirm account/instrument resolution, a buy + sell round trip, audit-log fill prices and net-position capping; compare Saxo SIM prices with yfinance (CHANGE.md Q8). (4) OAuth app (authorization code + refresh token) so unattended runs survive the 24 h token. (5) Only after a successful SIM run: switch the shipped stocks config to Saxo and delete the XTB executor/client (+ their tests/config) — kept until then as the known-dead reference.

73. **Performance ledger + baselines (CHANGE.md P2)** ⏳ [CHANGE.md §4.2, logged 2026-09-27 after §7.71]
    - Per sleeve, net of fees: realized PnL, return on allocated capital, win rate, profit factor, average win/loss, max drawdown, trade count, average holding time — from the `strategy`-tagged orders/decisions and `sleeve_snapshots` §7.71 now records.
    - Baselines on the same capital and period: buy & hold of the sleeve's universe, a 20/50 MA crossover on the sleeve's timeframe, cash; a sleeve is only *eligible* for more than its floor weight if it beats the best baseline (input to the P3 allocator).
    - Extend the decision-replay backtester (§7.14) to replay per sleeve (today it replays every sleeve's decisions as one book) and to compute the baselines; dashboard: vs-baseline columns on the §7.71 sleeve table.
    - Needs ≥ 2 weeks of two-sleeve paper history (CHANGE.md P1 "done when") to say anything; the code can land before that.

28. **Keyed venue smoke pass** ⏳ [R1-H4, §7.6 follow-up, find #1] — *re-scoped by §7.41*
    - Kraken **spot has no sandbox**, so the original "Kraken testnet" run cannot exist. **Now concrete (2026-09-26):** run it on the **OKX Europe demo** — §7.64 landed, the shipped config is ready (`exchange: myokx`, `testnet: true`; put the demo API key + secret + passphrase in `.env` as `EXCHANGE_API_KEY`/`_SECRET`/`_PASSPHRASE`) — first `--once`, then a multi-day paper run; confirms order format, fills, reconciliation and fee reporting against a real API. Needs a network-enabled environment.
    - *Done meanwhile:* per-cycle status reconciliation of orders left `open` is implemented and pinned — `KrakenExecutor.reconcile_open_orders()` re-polls pending venue orders each cycle (agent-side `_reconcile_orders`), patches the stored row via `Storage.update_order_status`, and flows late fills through the FIFO ledger with entry-decision attribution (§7.28).

### Low severity / housekeeping (open)

72. **Strategy-sleeve follow-ups** ⏳ [low, found while building §7.71, 2026-09-27]
    - A *pending* (not yet filled) venue BUY is not in the FIFO ledger, so it does not lock its symbol until it fills — another sleeve could enter the same symbol in that window. Paper fills instantly and OKX market orders fill at once, so the window is small; revisit with the §7.28 keyed run (claim the symbol from `CcxtExecutor`'s pending orders).
    - The dashboard's owner column reads the latest tagged BUY per symbol (the runner derives ownership from the ledger) — identical under the symbol lock, but a legacy untagged position shows `—` there while the runner assigns it to the first sleeve.
    - A sleeve re-baseline (`rebaseline_drawdown.py --strategy`) and a weights change take effect at the next runner start, like the agent-level §7.53 reset.


18. **Data-enrichment feeds** ⏳ (optional)
    - Sentiment provider (crypto) and economic-calendar feed (stocks) are aspirational context enrichments.
    - Expanded into a full proposal (news/events ingest, context cards, screener, watchlist) in [CHANGE.md](CHANGE.md) §4.4.

34. **Venue-side stop orders (OCO)** ⏳ [§7.9 follow-up]
    - SL/TP enforcement is local to the agent; venue-side OCO stop orders (OKX demo algo orders, later Saxo) remain future work — worth revisiting together with the §7.28 keyed run.



