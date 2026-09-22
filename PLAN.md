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
| `review.MD` / `review2.md` / `external_review3.md` | external full-codebase reviews (`[R-xx]` tags reference these) |

**Numbering rule:** §7.N identifiers (§7.1–§7.38) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-22):** 553 tests passing at ~94% coverage, zero pytest warnings. All of §7 except §7.18 (optional enrichment), §7.28 (keyed Kraken-testnet run — needs a network-enabled environment) and §7.34 (venue-side OCO) is complete (see [HISTORY.md](HISTORY.md)). The open items reflect all open work consolidated from `review.MD`, `review2.md`, `external_review3.md`, and `nightly_finds.md`, sorted by severity.

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

Updated after the full-codebase reviews of **2026-09-15** (`review.MD`), **2026-09-17** (`review2.md`), and **2026-09-21** (`external_review3.md`). Bugs and gaps found during development are logged in `nightly_finds.md`. Overlaps have been consolidated and all open items are grouped by severity below.

> **This section lists only open work.** Items §7.1–§7.27, §7.29–§7.33, §7.35 and §7.37–§7.38 were completed in 2026-09; their full write-ups live in [HISTORY.md](HISTORY.md) under their original numbers. §7.N identifiers are **never renumbered or reused**.

### Medium severity (open)

28. **Live Kraken testnet smoke pass** ⏳ [R1-H4, §7.6 follow-up, find #1]
    - A keyed run against the Kraken testnet from a network-enabled environment is pending (the dev sandbox blocks outbound HTTPS).
    - *Done meanwhile:* per-cycle status reconciliation of orders left `open` is implemented and pinned — `KrakenExecutor.reconcile_open_orders()` re-polls pending venue orders each cycle (agent-side `_reconcile_orders`), patches the stored row via `Storage.update_order_status`, and flows late fills through the FIFO ledger with entry-decision attribution (§7.28).





### Low severity / housekeeping (open)

18. **Data-enrichment feeds** ⏳ (optional)
    - Sentiment provider (crypto) and economic-calendar feed (stocks) are aspirational context enrichments.

34. **Venue-side stop orders (OCO)** ⏳ [§7.9 follow-up]
    - SL/TP enforcement is local to the agent; venue-side OCO stop orders on Kraken/XTB remain future work.






