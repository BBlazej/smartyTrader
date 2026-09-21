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

**Numbering rule:** §7.N identifiers (§7.1–§7.24) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-21):** 504 tests passing at ~93% coverage, zero pytest warnings. §7.1–§7.17 and §7.19–§7.27, §7.29 and §7.30 are complete (see [HISTORY.md](HISTORY.md)). Open items §7.18, §7.28 and §7.31–§7.38 reflect all open work consolidated from `review.MD`, `review2.md`, `external_review3.md`, and `nightly_finds.md`, sorted by severity.

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
| LLM non-determinism | Backtest replay won't reproduce stored decisions | `temperature=0.2` set (not 0); **no seed** — pin `temperature=0` and a seed where the model allows, and log the full prompt+response for audit (still open — see §7.33) |
| Paper PnL is optimistic | Overstates strategy quality | Verify `PaperExecutor` fee/slippage defaults before trusting paper PnL against the §4.3 live-readiness gates |

---

## 7. Gaps & Next Steps

Updated after the full-codebase reviews of **2026-09-15** (`review.MD`), **2026-09-17** (`review2.md`), and **2026-09-21** (`external_review3.md`). Bugs and gaps found during development are logged in `nightly_finds.md`. Overlaps have been consolidated and all open items are grouped by severity below.

> **This section lists only open work.** Items §7.1–§7.27, §7.29 and §7.30 were completed in 2026-09; their full write-ups live in [HISTORY.md](HISTORY.md) under their original numbers. §7.N identifiers are **never renumbered or reused**.

### Medium severity (open)

28. **Live Kraken testnet smoke pass & order reconciliation** ⏳ [R1-H4, §7.6 follow-up, find #1]
    - A keyed run against the Kraken testnet from a network-enabled environment is pending (the dev sandbox blocks outbound HTTPS). Per-cycle status reconciliation of orders left `open` remains unpinned.


31. **End-to-end Control API <-> Agent loop integration test** ⏳ [R2-3.1]
    - Write an integration test in `tests/integration/` verifying that `POST /api/agents/crypto/pause` and `close-all` pause cycles or execute emergency close in a running `BaseTradingAgent`.

32. **APScheduler concurrency, misfire, and overlap tests** ⏳ [R2-3.2]
    - Add unit/integration tests in `tests/unit/test_scheduler.py` covering job misfire policies, cycle overlap prevention, and concurrent execution under errors.

33. **LLM response size guards and config seed parameter** ⏳ [R2-2.2, R2-2.4]
    - Expose optional `seed` parameter in `LLMSettings` and `config/settings.yaml` for deterministic evaluation; add raw response size upper-bound checks in `llm_client.py` before parsing.

### Low severity / housekeeping (open)

18. **Data-enrichment feeds** ⏳ (optional)
    - Sentiment provider (crypto) and economic-calendar feed (stocks) are aspirational context enrichments.

34. **Venue-side stop orders (OCO)** ⏳ [§7.9 follow-up]
    - SL/TP enforcement is local to the agent; venue-side OCO stop orders on Kraken/XTB remain future work.

35. **SQLite automated point-in-time database backup (`.backup`)** ⏳ [R2-4.1]
    - Add an `aiosqlite` `.backup()` call during retention pruning in `core/retention.py` to create timestamped database backups.

36. **Storage repository sub-module decomposition (`storage.py`)** ⏳ [R2-4.2]
    - Split `storage.py` into sub-modules under `core/storage/` (`migrations.py`, `snapshots.py`, `decisions.py`, `control.py`) while keeping `Storage` as an orchestrating facade.

37. **Calendar-aware Sharpe annualization & indicator math optimizations** ⏳ [R3-L3, R3-L4, find #3, find #11]
    - Adjust `_PERIODS_PER_YEAR` in `backtester.py` for stock trading calendars (~252 days/year); optimize $O(N^2)$ MACD loop in `analysis/indicators.py` to $O(N)$; add epsilon tolerance to daily loss float boundary check.

38. **Short-side position model support & multi-side FIFO tracking** ⏳ [R3-L2, find #4, find #9]
    - Add an explicit `side` (long/short) field to `Position` model in `core/models.py` and extend `PositionTracker` for short opening/closing lots if margin/derivatives trading is added.

