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

**Numbering rule:** §7.N identifiers (§7.1–§7.23) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. §7 lists only open work: completed items live in [HISTORY.md](HISTORY.md) under their original numbers.

**Current state (2026-09-17):** 414 tests passing at ~94% coverage. §7.1–§7.14 and §7.20–§7.23 are complete (see HISTORY.md). Open: §7.15 P3–P5 (dashboard pages, config UI, Docker), §7.16 XTB OAuth2, §7.17–§7.19 housekeeping.

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

> **This section lists only open work.** Items §7.1–§7.14 and §7.20–§7.23 were completed in 2026-09; their full write-ups (with test counts) live in [HISTORY.md](HISTORY.md) under the same §7.N numbers — no stubs are kept here.

### Follow-ups carried from completed items

- **§7.6 live smoke:** a keyed run against the Kraken testnet from a network-enabled environment is still pending (the dev sandbox blocks outbound HTTPS — `nightly_finds.md` #1); per-cycle reconciliation of orders left `open` remains unpinned.
- **§7.9 venue-side stops:** SL/TP enforcement is local to the agent; venue-side OCO stop orders remain future work.

### C. Medium severity (open)

15. **Dashboard + control (Docker WebUI)** 🔨 in progress (P1+P2 done: control table + agent checks + control API; P3–P5 remaining: dashboard pages, config UI, Docker) — design in [ARCHITECTURE.md](ARCHITECTURE.md) — "Data pipeline, storage & dashboard"
   - **Stack (locked):** FastAPI + Jinja2/HTMX; uPlot charts via CDN; one slim Docker image (no Node build). Monitor + control + safe config.
   - **Control scope (locked):** Pause/Resume, Close-all, and a **safe config editor** (intervals, pairs/symbols, `risk.*`, `execution.*`, `monitoring.*`, `decision_history_limit`). **No** manual order placement, no live risk-param override, no kill. **No credentials/keys** — never read, written, or returned.
   - **Control channel (locked):** agent serves a small FastAPI control API; dashboard calls it (real-time, not gated on the 5-min cycle). DB remains the single source of truth via the new `agent_control` table.
   - **Phased build:**
     - P1 — shared state — ✅ **done** (WAL §7.15-preq; `agent_control` table keyed by agent name with `state`/`close_all_requested`/`last_cycle_at`/`last_error`/`config_override_json` + repository upserts in `storage.py`). The agent checks its control row at the top of every cycle (`BaseTradingAgent._handle_control`): overrides applied first, then close-all (executes **even while paused**, through `DecisionPipeline.close_all_positions` — no LLM, no risk gate, latch cleared after attempt), then the pause skip; a heartbeat (`last_cycle_at`/`last_error`) is stamped after each normal cycle. All checks are strict (`is True` / equality) and the read is fail-soft — a broken control plane never halts trading nor fabricates actions.
     - P2 — control API — ✅ **done** (`src/core/control_api.py::create_control_app`, started in-process by `core/runner.py` when `control_api.enabled`; loopback-bound, off by default; ports `crypto 8101 / stocks 8102`). Endpoints: `GET /api/agents[/{agent}]` (state, positions, portfolio, recent decisions, errors), `/decisions`, `/portfolio`, `POST pause|resume|close-all` (all write the DB latch — never orders), `GET/PUT /api/config`. **No credentials ever:** responses are field-built from safe models; `PUT` accepts only `SafeConfigOverrides` (`extra="forbid"` rejects unknown/credential keys wholesale); applied to live objects next cycle via `control_config.parse_and_apply` (risk thresholds mutate the shared `RiskSettings`; paper fee/slippage land on the executor; `interval_minutes` applies at restart — documented).
     - P3 — dashboard (monitor): FastAPI + Jinja2/HTMX pages — portfolio history (uPlot), positions, decisions + win-rate/confidence, agent health. (Tests: seeded-DB render.)
     - P4 — dashboard (control + config): Pause/Resume/Close-all buttons; safe config form with server-side Pydantic validation → `agent_control` overrides. (Tests: config whitelist rejects credentials.)
     - P5 — Docker: `Dockerfile` (python:3.11-slim) + `docker-compose.yml` (agent-crypto, agent-stocks, backtester, dashboard) with the shared `agent-data` + `agent-config` volumes.
   - **Acceptance:** from a browser — see live portfolio value/decisions/health; pause & resume the agent and watch it stop/start; close all open positions; change a safe config value and see it take effect on the next cycle. Credentials are never exposed.

16. **XTB demo OAuth2 flow** ⏳
   - `run_stocks_agent.py` currently falls back to the paper executor until the OAuth2 flow lands. Required before real XTB demo trading; the stocks *agent* and *provider* logic is already built and tested against the paper executor.

### D. Low severity / housekeeping

17. **Split `analysis/` out of `core/decision_pipeline.py`** (housekeeping)
   - Indicators (`compute_indicators`, `_compute_rsi`, `_compute_macd`, `_compute_bollinger_bands`) and the default system prompt currently live in `core/decision_pipeline.py`. Move to `analysis/indicators.py` + `analysis/prompt_builder.py` for the plan's intended layout — safe to do now that the prompt work has landed (§7.20).

18. **Data-enrichment feeds** ⏳ (optional, lower priority)
   - Sentiment provider (crypto) and economic-calendar feed (stocks) are still aspirational. Ship the higher-priority items above first; add these as the prompt benefits from richer context.

19. **Code nits bundle** ⏳ **[R-L]** — agents call `pipeline._get_portfolio_state()` (private) in both agents and tests, pinning an encapsulation leak into the test contract; `_check_cooldown` reaches into `_loss_tracker._cooldown_until` with a `# type: ignore`; `DailyLossTracker.daily_portfolio_value -> float` returns `None` via a `getattr` hack; hardcoded values vs the "config-driven" rule: consecutive-loss threshold 3 (`risk_engine.py:67`), LLM `temperature=0.2`/`max_tokens=1024`, paper `initial_cash` (→ §7.7); LLM client retries with **no backoff** and derives base_url via `endpoint.rsplit("/v1", 1)[0]` then re-appends `/v1/chat/completions` — works with the shipped config, fragile otherwise; `risk_engine.py`/`llm_client.py` use stdlib logging while everything else uses structlog (safety-critical rejection logs bypass the configured renderers); `PipelineStep(str)` should be a real `Enum`; sells are sized at `max_position_pct × total_value` regardless of units held → guaranteed-rejected sell loops, and float equality (`pos.quantity == 0`) for position cleanup can strand dust positions that count toward `max_open_positions`; RSI/ATR use simple averages rather than Wilder smoothing — values differ from TradingView/pandas-ta readings, so document them to avoid misreading LLM-facing numbers.
