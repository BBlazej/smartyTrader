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

**Numbering rule:** §7.N identifiers (§7.1–§7.60) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-24):** 649 tests passing at ~94% coverage, zero pytest warnings. §7.1–§7.39 are complete except §7.18 (optional enrichment), §7.28 (keyed Kraken run — premise corrected by §7.41: Kraken spot has no sandbox) and §7.34 (venue-side OCO) (see [HISTORY.md](HISTORY.md)). External review 4 (`external_4.md`, 2026-09-24) opened §7.39–§7.60. Done so far: §7.39 (per-agent storage scoping), §7.42 (per-position cap), §7.43 (browser-safe dashboard/control API, tighten-only risk overrides), §7.44 (fail-soft, lossless post-order persistence), §7.45 (book-aware prompt), §7.46 (loss streak counted once per closing fill), §7.47 (exits never gated; SELL closes in full) and §7.56 (configurable timeframe, one decision per closed bar). Two critical findings remain open (§7.40 XTB sells opening shorts, §7.41 Kraken keyed path) and must land before any venue execution. The open items reflect all open work consolidated from `review.MD`, `review2.md`, `external_review3.md`, `external_4.md`, and `nightly_finds.md`, sorted by severity.

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

> **This section lists only open work.** Items §7.1–§7.27, §7.29–§7.33, §7.35 and §7.37–§7.39, §7.42–§7.47 and §7.56 were completed in 2026-09; their full write-ups live in [HISTORY.md](HISTORY.md) under their original numbers. §7.N identifiers are **never renumbered or reused**.

### Critical / high severity (open)

> Order of work (from `external_4.md` §7; §7.39, §7.42–§7.47, §7.56 done): §7.51 next before starting the §4.3 paper clock; §7.49 before trusting any replay numbers; §7.40/§7.41/§7.48/§7.58 before any venue work.

40. **XTB: SELL must close, not open a short** ⏳ [R4-C2, R4-L9] — *critical*
    - `xtb_client.create_order` always sends `tradeTransaction` `type=OPEN`; `cmd=SELL, type=OPEN` opens a short. LLM sells, §7.9 auto-exits and close-all therefore open shorts while the local FIFO tracker books a fictional realized PnL (feeding the loss streak and the prompt).
    - Fix: track open xAPI trade `order` ids per symbol (`getTrades`) and route closing sells to `type=CLOSE` (2) against them (split volume as needed); refuse short-opening sells unless a future margin mode opts in. Also re-ground `_REQUEST_STATUS_MAP` (codes 2/5/6 appear not to exist). Transport-fake test asserting the CLOSE payload.

41. **Kraken keyed path: no spot sandbox exists; guard live trading; honest spot valuation** ⏳ [R4-C3, R4-L7, amends §7.28 / find #1] — *critical*
    - ccxt `kraken` has no `urls['test']`: `set_sandbox_mode(True)` raises `TypeError` at startup (fails closed). The only workaround, `testnet: false`, trades **production funds** while logging "Kraken testnet executor". Only `krakenfutures` has a demo environment.
    - On spot, `get_positions()` is always `[]` and `get_cash()` is free quote only, so every BUY registers as a loss of its own notional (trips daily-loss + drawdown at once and poisons the persisted peak); SL/TP are never enforced, close-all is a no-op, SELL sizing is unclamped.
    - Fix: relabel the keyed path as live and require an explicit acknowledgement (config flag + env flag) — same for `xtb_execution.account_type: real`; derive spot positions from the local FIFO ledger + `fetch_balance()` totals marked at the snapshot close; if a sandbox is required, target `krakenfutures` demo (makes the §7.38 short model load-bearing). Rewrite §7.28 accordingly.

48. **Side-aware closes and exit levels** ⏳ [R4-H6]
    - Since §7.38 venues report `side=SHORT`, but `close_all_positions` and `_check_exit_levels` always SELL and `exit_level_breach` is long-only — closing a short increases it. Reachable on XTB today via §7.40.
    - Fix: cover shorts with BUY and mirror breach semantics (or skip shorts with a warning while spot-only); tests with `side=SHORT`.

49. **Backtester look-ahead bias** ⏳ [R4-H7]
    - Candle timestamps are *open* times, but `_build_timeline` fires the candle event at its open and uses its close for marks, exits and decision pricing — a 10:00 decision on `1d` stocks fills at the 16:30 close. Replay PnL is systematically optimistic.
    - Fix: candle events at `open_ts + timeframe`, or price decisions at the last completed candle; intra-candle decision test.

### Medium severity (open)

28. **Keyed Kraken smoke pass** ⏳ [R1-H4, §7.6 follow-up, find #1] — *premise corrected by §7.41*
    - Kraken **spot has no sandbox** (ccxt `urls['test']` is `None`), so a "testnet" smoke run as originally planned cannot exist; re-scope after §7.41 (either a futures-demo target or an explicitly acknowledged minimal-size live run). The dev sandbox also blocks outbound HTTPS.
    - *Done meanwhile:* per-cycle status reconciliation of orders left `open` is implemented and pinned — `KrakenExecutor.reconcile_open_orders()` re-polls pending venue orders each cycle (agent-side `_reconcile_orders`), patches the stored row via `Storage.update_order_status`, and flows late fills through the FIFO ledger with entry-decision attribution (§7.28).

50. **Safe-config overrides that never apply** ⏳ [R4-M1] (same class as find #16)
    - `market_hours`: written to `settings.stocks_agent`, but `StocksAgent` reads its constructor copy `self._market_hours` — no effect.
    - `interval_minutes`: documented "applies at restart" but never applies — the runner schedules from YAML before the first cycle applies overrides (the real DB holds an unhonored `interval_minutes: 1`).
    - Removing an override does not revert live objects; the dashboard form persists every displayed field, pinning all risk defaults so later stricter YAML edits are silently overridden.
    - Fix: single reader per field (agent reads `settings.*` per cycle), reschedule the APScheduler job on interval change, re-apply YAML base + overrides on every change (including removal), form submits only fields that differ from YAML.

51. **Make LLM outages visible; real alert sink** ⏳ [R4-M2]
    - A fallback HOLD is not a cycle error: `last_error` stays `None`, dashboard shows `running`, no alert. 56% of stored decisions are fallbacks (`All connection attempts failed`) and nothing surfaced it; alerts only reach `NoopAlertSink`.
    - Fix: `is_fallback` → cycle error; fallback-rate on the dashboard; at least one real sink (email/ntfy/webhook) before §4.3.

52. **Single-instance lock per agent runner** ⏳ [R4-M3]
    - Nothing prevents two runners of the same agent (separate in-memory paper books, shared DB). The dashboard's fresh-heartbeat twin check misses a runner still in rehydration or on a slow first LLM call.
    - Fix: exclusive `fcntl` lock on `data/<agent>.lock` (or a PID+TTL lease row in `agent_control`) at `run_agent` startup.

53. **Drawdown peak re-baseline procedure** ⏳ [R4-M4]
    - Peak = all-time `MAX(total_value)` over never-pruned snapshots → the drawdown gate is a permanent latch with no documented exit other than hand-editing the DB (§7.41 can trip it spuriously).
    - Fix: explicit, audited CLI-only "re-baseline peak" action + documented procedure.

54. **Validate entry SL/TP geometry and risk per trade** ⏳ [R4-M5]
    - BUY only needs `stop_loss is not None`: SL ≥ price or TP ≤ price is auto-closed next cycle (double fees); SL at `0.01` means unbounded risk; sizing ignores stop distance.
    - Fix: require `stop_loss < price < take_profit`, cap `(price − SL)/price` via config; optional risk-per-trade sizing.

55. **Refuse to trade without a price** ⏳ [R4-M6]
    - Empty candles → `planned_notional=None` skips the notional cap, quantity sized at `stop_loss` (or price 1.0), sent with `price=None`; `KrakenExecutor` turns that into an unchecked **market** order.
    - Fix: no candles → no LLM call, no order.

57. **LLM response parser robustness** ⏳ [R4-M8]
    - `_parse_signal` only strips code fences; Qwen3-family `<think>…</think>` or leading prose fails every attempt → HOLD fallback; `max_tokens: 1024` can truncate JSON after long reasoning.
    - Fix: strip think blocks, extract the last balanced `{…}`; enable `use_json_schema` when supported; think-prefixed parser tests.

58. **Venue executor restart rehydration** ⏳ [R4-M9] (find #7 / §7.25 covered paper only)
    - `KrakenExecutor`/`XTBExecutor` restart with empty FIFO ledgers and empty `_exit_levels` (pre-restart stops unenforced, closes report no outcome); `KrakenExecutor._open_orders` is memory-only, so an order open across a restart stays `pending` in the DB forever.
    - Fix: `load_fills()` hook fed by `get_filled_orders` for the agent's symbols; persist exit levels with the order/decision; reload `status='pending'` rows into `_open_orders` at startup.

### Low severity / housekeeping (open)

18. **Data-enrichment feeds** ⏳ (optional)
    - Sentiment provider (crypto) and economic-calendar feed (stocks) are aspirational context enrichments.

34. **Venue-side stop orders (OCO)** ⏳ [§7.9 follow-up]
    - SL/TP enforcement is local to the agent; venue-side OCO stop orders on Kraken/XTB remain future work.

59. **Review-4 low-severity bundle** ⏳ [R4-L1–L3, L5, L6, L8, L10, L11]
    - L1: sizing ignores fee + slippage → cash-bound paper buys are rejected after approval; size against `price × (1+slippage) × (1+fee)`.
    - L2: `round(quantity, 8)` after the holdings clamp can round a sell above holdings; round down.
    - L3: first risk check after UTC midnight compares against yesterday's baseline (`reset_if_new_day` only runs in post-processing); roll the day at the top of `_check_daily_loss`.
    - L5: prompt prices/candles now keep significant digits for sub-$1 assets (§7.45/§7.56); indicator values (ATR, Bollinger) still round to 2 dp in `compute_indicators`.
    - L6: compose — disabled `agent-stocks` with `restart: unless-stopped` restart-loops; compose passes `XTB_API_KEY` but the runner reads `XTB_ACCOUNT_ID`/`XTB_ACCOUNT_PASSWORD`.
    - L8: no data↔execution symbol mapping (yfinance `AAPL` vs xAPI `AAPL.US`-style names); make it a config table.
    - L10: doc drift — AGENTS.md pidfile `data/<agent>.pid` vs code `data/<agent>_agent.pid` (the duplicated ARCHITECTURE risk-engine sentence was fixed with §7.47).
    - L11: orphaned `agent_control` row `crypto_agent` (pre-find-#16) in existing DBs; stale untracked `build/lib/` copy of pre-§7.36 code (`rm -rf build/`).

60. **Continuous integration** ⏳ [R4-L12]
    - No CI exists; local venv is Python 3.14 while the image is 3.11. Add a minimal job (ruff check + format check + pytest on 3.11) so the documented test counts are reproducible.
