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
| `nightly_finds.md` | bugs/gaps discovered during development (numbered findings) |
| `review.MD` / `review2.md` / `external_review3.md` / `external_4.md` | external full-codebase reviews (`[R-xx]` tags reference these; `[R4-xx]` = `external_4.md`) |

**Numbering rule:** §7.N identifiers (§7.1–§7.61) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-25):** 788 tests passing at ~94% coverage, zero pytest warnings — on the local venv and on a clean Python 3.11 install running the CI job's steps (the GitHub workflow itself first runs on the next push). §7.1–§7.38 are complete except §7.18 (optional enrichment), §7.28 (keyed Kraken run — premise corrected by §7.41: Kraken spot has no sandbox) and §7.34 (venue-side OCO) (see [HISTORY.md](HISTORY.md)). External review 4 (`external_4.md`, 2026-09-24) opened §7.39–§7.60. Done so far: §7.39 (per-agent storage scoping), §7.40 (XTB closes via type=CLOSE, never flips), §7.41 (no accidental live Kraken trading; honest spot valuation), §7.42 (per-position cap), §7.43 (browser-safe dashboard/control API, tighten-only risk overrides), §7.44 (fail-soft, lossless post-order persistence), §7.45 (book-aware prompt), §7.46 (loss streak counted once per closing fill), §7.47 (exits never gated; SELL closes in full), §7.48 (side-aware closes and exit levels), §7.49 (backtester look-ahead removed), §7.50 (safe-config overrides actually apply — live interval reschedule, baseline+override re-apply, removal reverts, diff-only persistence), §7.51 (LLM outages surfaced; webhook alert channel), §7.52 (single-instance runner flock), §7.53 (audited CLI drawdown re-baseline), §7.54 (entry SL/TP geometry + optional risk-per-trade sizing), §7.55 (no price → no LLM call, no order), §7.56 (configurable timeframe, one decision per closed bar), §7.57 (reasoning-model tolerant LLM parser), §7.58 (venue executors rehydrate ledgers, exit levels and pending orders) §7.59 (review-4 low-severity bundle: cost-aware sizing, midnight rollover, sub-$1 indicators, compose, XTB symbol map, doc/DB housekeeping), §7.60 (CI on Python 3.11 — which caught a missing `greenlet` dependency, find #20) and §7.61 (venue-tagged rows; partial fills recorded). All four critical findings are closed; no venue execution path can reach real money without `live_trading` + `LIVE_TRADING_ACK`. The open items reflect all open work consolidated from `review.MD`, `review2.md`, `external_review3.md`, `external_4.md`, and `nightly_finds.md`, sorted by severity.

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

Updated after the full-codebase reviews of **2026-09-15** (`review.MD`), **2026-09-17** (`review2.md`), **2026-09-21** (`external_review3.md`), and **2026-09-24** (`external_4.md` — §7.39–§7.60). Bugs and gaps found during development are logged in `nightly_finds.md`. Overlaps have been consolidated and all open items are grouped by severity below.

> **This section lists only open work.** Items §7.1–§7.27, §7.29–§7.33, §7.35 and §7.37–§7.61 were completed in 2026-09; their full write-ups live in [HISTORY.md](HISTORY.md) under their original numbers. §7.N identifiers are **never renumbered or reused**.

### Critical / high severity (open)

> Order of work (from `external_4.md` §7; §7.39–§7.58 done — the §4.3 paper clock can start, replay numbers are look-ahead-free and the venue prerequisites §7.40/§7.41/§7.48/§7.58 are in). Nothing critical/high remains open; §7.61 (venue history hygiene, finds #18/#19) also landed ahead of the keyed venue run (§7.28).

### Medium severity (open)

28. **Keyed venue smoke pass** ⏳ [R1-H4, §7.6 follow-up, find #1] — *re-scoped by §7.41*
    - Kraken **spot has no sandbox**, so the original "Kraken testnet" run cannot exist. Options now: (a) a keyed run on an exchange ccxt has a sandbox for (`testnet: true` → `<exchange>-sandbox` mode), or (b) a deliberately acknowledged minimal-size live Kraken spot run (`testnet: false` + `live_trading: true` + `LIVE_TRADING_ACK`). Either needs a network-enabled environment (the dev sandbox blocks outbound HTTPS).
    - *Done meanwhile:* per-cycle status reconciliation of orders left `open` is implemented and pinned — `KrakenExecutor.reconcile_open_orders()` re-polls pending venue orders each cycle (agent-side `_reconcile_orders`), patches the stored row via `Storage.update_order_status`, and flows late fills through the FIFO ledger with entry-decision attribution (§7.28).

### Low severity / housekeeping (open)

18. **Data-enrichment feeds** ⏳ (optional)
    - Sentiment provider (crypto) and economic-calendar feed (stocks) are aspirational context enrichments.

34. **Venue-side stop orders (OCO)** ⏳ [§7.9 follow-up]
    - SL/TP enforcement is local to the agent; venue-side OCO stop orders on Kraken/XTB remain future work.

