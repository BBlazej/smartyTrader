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
| `review.MD` / `review2.md` | external full-codebase reviews (`[R-xx]` tags reference these) |

**Numbering rule:** §7.N identifiers (§7.1–§7.24) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-21):** 488 tests passing at ~93% coverage. §7.1–§7.17, §7.19–§7.23 are complete — including the dashboard + Docker packaging (§7.15), real XTB demo execution over xAPI (§7.16), the `analysis/` layer extraction (§7.17) and the code-nits bundle (§7.19) — plus §7.24 (start/stop agents from the dashboard, opt-in process supervision; see HISTORY.md). Open: §7.18 (optional enrichment — needs a provider decision).

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
| LLM non-determinism | Backtest replay won't reproduce stored decisions | `temperature=0.2` set (not 0); **no seed** — pin `temperature=0` and a seed where the model allows, and log the full prompt+response for audit (still open) |
| Paper PnL is optimistic | Overstates strategy quality | Verify `PaperExecutor` fee/slippage defaults before trusting paper PnL against the §4.3 live-readiness gates |

---

## 7. Gaps & Next Steps

Updated after the full-codebase reviews of **2026-09-15** and **2026-09-17** — findings are tagged **[R-xx]** referencing `review.MD` and `review2.md` at the repo root (H = high, M = medium, L = low severity there). Bugs/gaps found during development are logged separately in `nightly_finds.md`. Items marked ⏳ are planned/not yet implemented. The original sections **A. Low-hanging fruit** and **B. High severity** completed fully in 2026-09; what remains keeps the original severity grouping (**C. Medium → D. Low/housekeeping**) plus carried follow-ups.

> **This section lists only open work.** Items §7.1–§7.17, §7.19–§7.23 and §7.24 were completed in 2026-09; their full write-ups (with test counts) live in [HISTORY.md](HISTORY.md) under the same §7.N numbers — no stubs are kept here.

### Follow-ups carried from completed items

- **§7.6 live smoke:** a keyed run against the Kraken testnet from a network-enabled environment is still pending (the dev sandbox blocks outbound HTTPS — `nightly_finds.md` #1); per-cycle reconciliation of orders left `open` remains unpinned.
- **§7.9 venue-side stops:** SL/TP enforcement is local to the agent; venue-side OCO stop orders remain future work.

### C. Medium severity (open)

### D. Low severity / housekeeping

18. **Data-enrichment feeds** ⏳ (optional, lower priority)
   - Sentiment provider (crypto) and economic-calendar feed (stocks) are still aspirational. Ship the higher-priority items above first; add these as the prompt benefits from richer context.
