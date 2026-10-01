# Autonomous Trading Agent — Plan (open work)

The **single list of open work**: every todo, gap, open question and accepted limitation lives here — nowhere else. Delivered work is in [HISTORY.md](HISTORY.md), how the system is built in [ARCHITECTURE.md](ARCHITECTURE.md), the multi-strategy design rationale in [CHANGE.md](CHANGE.md).

**Numbering rule:** §7.N identifiers (§7.1–§7.91) are referenced across code comments, `AGENTS.md`, `README.md` and `HISTORY.md` — **never renumber or reuse them**. A finished item moves to HISTORY under its number; new work gets the next free number, a severity and a place in the order below.

**Current state (2026-10-01):** 1333 tests passing at ~95 % coverage, zero pytest warnings; ruff and the Python 3.11 CI checks green. Paper trading runs end to end on both markets; keyed execution is verified on the OKX demo; Saxo SIM is built but unverified.

---

## Order of work

| # | Item | Severity | Blocked on |
|---|---|---|---|
| 1 | §7.28 Multi-day OKX demo run | high | — |
| 2 | §7.85 LLM latency benchmark on real prompts | medium | LM Studio up |
| 3 | §7.86 Two-sleeve paper trial (≥ 2 weeks) | medium | — (runs alongside §7.28) |
| 4 | §7.80 Summarizer live pass + model choice | medium | LM Studio up; after §7.85 |
| 5 | §7.66 Saxo SIM run, then retire XTB | medium | a Saxo developer account |
| 6 | §7.74 Deterministic allocator | medium | §7.86 data |
| 7 | §7.84 `market_snapshots`: drop or give it a reader | low | a decision |
| 8 | §7.34 Venue-side stop orders (OCO) | low | after §7.28 |
| 9 | §7.81 Macro calendar upkeep | low | before 2027-12 / if the feed fails |
| 10 | §7.90 Repeated venue rejections | low | — |
| — | Live-readiness gate (below) | gate | §7.28, §7.86, §7.74 |

---

## Open items

### §7.28 — Multi-day OKX demo run ⏳ [high; R1-H4, re-scoped by §7.41/§7.64]

- **Done so far:** keys, reads, the overnight run, forced and agent-driven round trips on the OKX Europe demo — they found and fixed §7.75, §7.76, §7.77 and §7.79 (run log: HISTORY *§7.28 — keyed demo runs*). Fees are confirmed (taker 0.20 %, the paper profile matches).
- **Execution cost seen on the demo (test-profile run 2026-10-01, 30 round trips):** BUY fills sat a median **+0.20 %** above the reference close (the full `entry_offset_pct`) and market SELLs **−0.24 %** below it. That is ≈ 0.45 % slippage plus 0.40 % fees per round trip, so 29 of 30 trades lost money before fees and −€50.74 after (equity −2.6 %). Exits are judged on live public prices but fill on the demo book: a `take_profit` exit (SUI, mark above its TP) still lost money. Check during the multi-day run: is the demo book's spread representative of live? If it is, `entry_offset_pct` and the market SELL alone eat ~0.45 % per trade, and any strategy needs a larger expected move than that.
- **Reset done (2026-10-01):** the books contaminated by §7.88 (and the test-profile run) were moved to `data/backups/reset-20261001-contaminated/`; the 3 working demo BUYs were cancelled at OKX. The demo account still holds the earlier runs' coins (untracked by the fresh book — the agent never sells them, §7.88) and €2,004 free cash, which becomes the new book's capital.
- **Next:** run the crypto agent on the demo for several days in a clean `data/demo_crypto.db` (`python -m scripts.run_crypto_agent --mode demo`, output redirected to a log). Check: LLM-driven entries and exits fill and reconcile, restarts rehydrate cleanly, heartbeat stays fresh, no errors/fallbacks, fees on the order rows. Expect `Event guard:` rejections around macro events — intended (§7.18).

### §7.85 — LLM latency benchmark on real prompts ⏳ [medium; §7.69 follow-up, CHANGE Q5]

- §7.69 built the tooling (`scripts/benchmark_llm.py`, per-decision latency/tokens, dashboard p50/p95). **First real numbers (2026-10-01, 8.5 h demo run, 20 calls):** median 21 s, max 33 s per decision — the crypto universe was raised from 2 to 12 pairs on that basis (≈ 4 min of LLM per bar, one 5-minute cycle).
- **Test-profile demo run (2026-10-01, 13:55–15:36 CEST, 12 pairs, 15 m bars, 85 calls):** latency is **bimodal**. p50 is 18 s, but p90 is 140 s, p95 166 s and the max 313 s. 15 of the 85 calls took over 100 s, with 4–7k completion tokens of reasoning, and 2 answers were truncated at `max_tokens` 8192 and retried (the cap was then raised to 16384, timeout 600 s — a capped answer now costs up to ~7 min). A full cycle therefore took ~13 min against a 5-minute interval (11 ticks skipped), so each symbol's SL/TP was checked only every ~13 min. NEAR's stop at 4.49 was hit on a ~4.5 % drop and filled at 4.28. **Options:** fewer pairs; cap the reasoning with `llm.reasoning` (§7.91 — `low`/`medium` cut the hard prompt from 203 s to 37–40 s with the same decision; choose after a run on it); or check exits for all symbols before the LLM pass rather than symbol by symbol (worth its own §7 number if chosen).
- **Do:** confirm with the dashboard's p50/p95 over a multi-day run with 12 pairs (prompts grow as history fills), or `scripts/benchmark_llm.py`. If p95 × pairs exceeds the 5-minute cycle, cycles overrun and exit checks wait — then trim pairs or lengthen the cycle. Size any watchlist extension (`max_dynamic_symbols`) and the summarizer budget from the same numbers.

### §7.86 — Two-sleeve paper trial ⏳ [medium; CHANGE P1 "done when"]

- Sleeves (§7.71–§7.73) are built but ship off, so they have no history.
- **Do:** enable `crypto_agent.sleeves` on the **paper** book (swing 1 h + position 4 h) for at least two weeks. Done when results split cleanly per sleeve on the dashboard, with vs-baseline numbers (`scripts/backtest.py --strategy NAME`). The data feeds §7.74 and the live-readiness gate.

### §7.80 — Summarizer live pass + model choice ⏳ [medium; CHANGE Q7]

- The §7.18 summarizer is tested against mocked replies only and ships off.
- **Do:** enable it on the paper book; check card quality and latency (`llm_exchange` lines with `purpose=context_card`), the injection filter's false-positive rate (`CardRejected … injection` warnings) and the decision-latency cost of the shared lock. Decide between the trading model with `llm: {max_tokens: 4096}` and a smaller second model (VRAM fit vs LM Studio model swapping — JIT load / idle-TTL unload / `lms` CLI — and what a swap costs).

### §7.66 — Saxo SIM run, then retire XTB ⏳ [medium; broker decided 2026-09-26]

- **Built (mocked tests only):** `SaxoClient`/`SaxoExecutor`, OAuth app + `scripts/saxo_login.py`, per-exchange trading windows, stocks market context (live-checked). Details: HISTORY *§7.66 — progress*.
- **Next (needs a Saxo developer account — none configured):**
  1. First SIM run: `--once` with a 24 h developer token (or `saxo_login` with an app); confirm account/instrument resolution, a buy + sell round trip, audit-log fill prices, net-position capping and the OAuth refresh/keep-alive against the real `/token` endpoint (the LIVE auth host `live.logonvalidation.net` is still unverified).
  2. Compare Saxo SIM prices with yfinance and decide the stocks data source (CHANGE Q8).
  3. Only after a successful SIM run: switch the shipped stocks config to Saxo and delete the XTB executor/client + tests/config (and the short-position support only XTB uses).
  4. Before running stocks unattended: set `context.http_user_agent` to a name + contact (SEC's request); fill `market_holidays` for any EU exchange added under `exchanges`.
- Scope stays **paper/SIM only** until the budget grows (the $1 minimum is ~1 %/side at €100 positions).

### §7.74 — Deterministic allocator ⏳ [medium; CHANGE P3, §4.3]

- Weekly job re-weighting sleeves for *new entries only* (never force-closes): score each sleeve on its trailing window (Sharpe-like on sleeve equity, net of fees; 0 unless it beats its best baseline — §7.73), shrink toward equal weights by sample size (`w = n/(n+k)·w_perf + k/(n+k)·w_equal`, k ≈ 30 closed trades), clamp to `[min_weight, max_weight]`, cap the change per rebalance (±10 pp), renormalize, and write an audited `strategy_allocations` row (inputs, scores, old → new; reason `rebalance`). A sleeve latched by its drawdown guard scores 0 until re-baselined.
- The job fetches history for the baselines (the dashboard is DB-only), so it also persists each sleeve's baseline scores; the dashboard sleeve table then gains vs-baseline columns.
- Operator pin (safe-config override, tighten-only spirit of §7.43) and `min_weight`/`max_weight` per sleeve (`sleeves.strategies.*.budget`).
- Done when a replay over the §7.86 history produces sane, slow-moving weights and the operator can pin them.

### §7.84 — `market_snapshots` is write-only ⏳ [low; found 2026-09-30]

- Every decision stores the candle series + indicators, retention prunes them after `snapshot_retention_days`, and nothing reads them (the backtester fetches fresh candles; the full prompt is in the `llm_exchange` log). **Decide:** drop the table, the write and the setting — or give it a reader (e.g. replay the exact candles a decision saw).

### §7.34 — Venue-side stop orders (OCO) ⏳ [low; §7.9 follow-up]

- SL/TP are local checks: while the agent is down, nothing protects a venue position. Add venue-side OCO stop orders (OKX algo orders, later Saxo) — kept in sync with the local levels and cancelled on every other close path.

### §7.81 — Macro calendar upkeep ⏳ [low; §7.18 follow-up]

- `macro_calendar.events` holds FOMC + ECB decisions through 2027-12 (a warning fires when none is left); CPI/NFP/PCE come only from the unofficial ForexFactory feed (bls.gov blocks scripted fetches). Extend the list before it runs out; add the BLS dates by hand if the feed proves unreliable.

### §7.90 — Repeated venue rejections ⏳ [low; found 2026-10-01, test-profile demo run]

- **Account-level refusal:** OKX refused every XRP/EUR order with `54092` ("complete the disclaimer confirmation" — an account setting, not an order problem). The agent retried at every new bar, and each try was an `error` alert plus an LLM call. **Do:** after a venue refusal that cannot succeed until the operator acts (54092-type codes), park the symbol for the run (skip before the LLM, one alert naming the fix) instead of retrying blind.
- **Insufficient balance:** two BUYs (XLM, ONDO) failed with `51008` (insufficient balance), although sizing clamps to free cash. Several limit BUYs working at once probably lock cash after the book was read. **Check:** whether sizing should subtract cash committed to working BUYs (`pending_buy_value`, §7.79).

---

## Live-readiness gate (before any real money)

Per sleeve (CHANGE P6), on forward paper/demo time — not replays alone. A sleeve that passes may get a small real allocation (opt-in, ack-gated as today, ≈ €1,000 budget).

- [ ] Paper PnL positive for ≥ 4 weeks, net of fees, and beating the best dumb baseline (§7.73)
- [ ] Win rate > 50 % after fees
- [ ] Max drawdown within the sleeve's limits
- [ ] LLM latency consistently below the timeout (§7.85); fallback HOLDs rare
- [ ] Every circuit breaker exercised: daily loss, drawdown latch + CLI re-baseline, cooldown, event guard, close-all
- [ ] Exchange rate limits understood and respected under the real cycle load
- [ ] Recovery tested: network outage, venue downtime, PC restart mid-order (rehydration + reconciliation)

---

## Backlog (unscheduled ideas)

Picked up only when they earn a §7 number and a place in the order.

- **Decision review loop:** export decisions + outcomes after 2–4 weeks of paper trading, find the failure modes, refine prompts/playbooks; consider fine-tuning only if the local model supports it.
- **Multi-timeframe context:** a daily/weekly trend summary in the position sleeve's prompt (CHANGE §4.5).
- **More inputs:** order-book imbalance; VWAP / volume profile.
- **Correlation-aware exposure:** cap concentrated risk across correlated assets (all-crypto books move together).
- **Regime detection:** trending vs ranging → sleeve weighting or playbook choice.
- **LLM replay backtests:** feed history to the model for fresh signals (non-deterministic and costly; decision replay stays the default).
- **Postgres:** only if multi-writer contention ever shows up (WAL + one writer per book should not).

## Accepted limitations (revisit if the trigger happens)

- **Market-hours guard:** half-day early closes are not expressible (whole days only); a weekend closure wins over an overnight window that wraps into Saturday — revisit for a 24 h-adjacent venue (§7.10).
- **Event guard is conservative:** a delisting notice naming `XYZ/USDT` also blocks `XYZ/EUR` (§7.18).
- **Third-currency venue fees** (neither base nor quote) are logged, not booked (§7.75).
- **Sharpe in replays** is coarse for gappy stock series (calendar-aware since §7.37, still approximate).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| LLM gives bad signals | Deterministic risk gate on every entry; conservative defaults; exits never gated |
| LLM too slow / overloaded (timeouts → fallback HOLDs) | Timeout + HOLD fallback, loud alerts (§7.51); size the universe from §7.85; shared LLM lock; smaller summarizer model (§7.80) |
| LLM non-determinism | `temperature 0.2`, optional `seed`; full prompt/response audit log; decision replay needs no LLM |
| Paper PnL optimistic / overfitting to paper | Venue-matched costs (§7.65/§7.75); judge on forward time, not replays; start small |
| Allocator chases noise | Minimum sample, shrinkage, caps, ±10 pp per rebalance, baseline eligibility (§7.74) |
| News prompt injection / stale news | Raw text never in the trading prompt; strict bounded cards with TTL; guards read calendar data only (§7.18) |
| Strategies interfere on one symbol | Symbol lock (§7.71) |
| Exchange rate limits / downtime | ccxt rate limiting, backoff, fail-soft cycles, reconciliation of working orders |
| Saxo SIM behaves differently from the mocks | §7.66 SIM run before any stocks use |
| Complexity outgrows the safety story | Every feature behind a flag; with flags off behaviour is unchanged |
