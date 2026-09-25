# CHANGE.md — Multi-strategy trading with a research layer (proposal)

**Status:** proposal, under discussion — nothing here is implemented. Started 2026-09-26.
**Decided so far (2026-09-26):** Q1 — venues: **OKX Europe** for crypto, **Saxo** for stocks
(demo/SIM first, same APIs for real money later; §8); Q2 — **crypto first**; Q3 — "long-term"
means **days to weeks** (position trading, not months-long investing); Q4 — risk limits move to
**per-style (per-sleeve) limits** plus one loose agent-wide backstop (§4.8); Q6 — **free news
sources only**. Kraken is dropped entirely (no fallback code).

**Constraints (2026-09-26):** real money at first **≈ €1,000 at most** (may grow after paper
trading earns trust) and the project should cost next to nothing — free services only, paper
cash sized to the real plan, real-money stocks deferred (Saxo's $1 minimum is ~1 %/side on
€100 trades). Everything runs on the user's **desktop PC** (same machine as LM Studio), so
restart-safety matters and GPU/electricity use is a real cost.
**Scope:** turn today's single-style swing trader into a system that runs several trading
*styles* side by side (short-term swing + longer-term position trades), measures which one
actually earns money, shifts capital toward what works, and widens what the agents look at
beyond candles and their own trade history (news, events, screeners).

When parts of this are accepted they become PLAN §7 items (and later HISTORY write-ups) like
any other work; this file is the design record and discussion space.

---

## 1. Where we are today

- **One style per agent.** Crypto decides once per closed **1 h** bar, stocks once per closed
  **1 d** bar. Positions are held until the LLM says SELL or a local SL/TP check fires — no
  holding-time limit, no notion of "this is a short trade" vs "this is a long hold".
- **Inputs are price-only:** RSI-14, MACD, Bollinger(20), ATR-14, volume SMA on closed bars, plus
  the agent's own book and last-N decisions with outcomes.
- **Fixed universe:** `crypto_agent.pairs` / `stocks_agent.symbols` in YAML (changeable via the
  safe-config override → `set_symbols`, but nothing chooses them automatically).
- **Risk limits are tuned for short-term trading:** 5 % max drawdown latch, 2 % daily loss,
  every entry needs a stop within 25 % of price, 10 % per position, 5 positions max.
- **Positions are keyed per symbol** everywhere (paper book, Kraken spot ledger, exit levels,
  XTB close logic). Two strategies holding the same symbol would collide — see §4.1.

## 2. Does the idea make sense?

**Yes, as a direction — with three corrections to the framing.**

1. **"Agents decide how profitable each approach is" → the system *measures* it; the LLM
   doesn't judge it.** An LLM cannot reliably estimate its own edge, and capital allocation is a
   risk control. Project rule: *risk rules are hard-coded deterministic guards, not LLM
   decisions.* So: the LLM proposes trades inside a strategy; deterministic code measures each
   strategy's realized, fee-inclusive results and moves budget between them slowly.
2. **"Shift money to what's profitable" is where most such systems lose money.** Recent
   profitability over a few dozen trades is mostly noise; chasing it means buying last month's
   luck. Allocation must be slow, shrunk toward equal weights, need a minimum sample, and be
   capped — and every strategy has to beat a *dumb baseline* (buy & hold, a moving-average
   rule) net of fees, or it gets nothing extra.
3. **More information ≠ more profit — and news is an attack surface.** News helps most with
   *what to look at* (universe selection) and *what to avoid* (earnings in two days, a
   delisting notice), and much less with timing — liquid markets price headlines in fast, and a
   local 27B model reading them minutes later is not first. Also: news text is **untrusted
   input to a model that can place orders** (prompt injection). It must reach the trading
   prompt only as structured, length-limited fields, never raw article text, and it can never
   trigger an order on its own.

**Expectation setting:** an LLM trading on technicals has no proven edge; the honest outcome
of this work may be "the long-hold sleeve beats the swing sleeve, and neither beats buy &
hold after fees". The architecture should make that answer cheap to get and easy to act on
(i.e. allocate to the baseline), not hide it.

## 3. Design principles (non-negotiable)

1. **LLM proposes, deterministic code disposes.** Allocation, sizing, universe limits, event
   blackouts and all risk gates are code + config. The risk engine still runs before every order.
2. **Everything is measured net of realistic costs** (taker fee + slippage per venue), against
   baselines, per strategy.
3. **Untrusted text never reaches the order path raw.** News → summarizer → structured
   "context card" (bounded fields, sources, timestamps) → trading prompt.
4. **Paper first, per strategy.** A strategy earns real capital only by passing the §4.3
   live-readiness gates on its own track record.
5. **Budget the local LLM.** Every new call competes for one GPU; decisions are sequential and
   can take minutes (`llm.timeout_seconds: 300`). Call volume is a design constraint (§4.7).
6. **Additive and switchable.** Each phase ships behind config flags; with everything off the
   system behaves exactly as today.

## 4. Target architecture

```mermaid
flowchart LR
  subgraph Research["Research layer (off the trade path)"]
    SCAN["Screener\n(deterministic: liquidity,\nmomentum, volume spikes)"]
    NEWS["News & event ingest\n(RSS/APIs, filings,\ncalendars)"]
    SUMM["Summarizer job (LLM, batch)\n→ context cards"]
    WL["Watchlist manager\n(deterministic, capped, TTL)"]
    SCAN --> WL
    NEWS --> SUMM --> WL
  end
  subgraph Trading["Trading layer (per strategy sleeve)"]
    S1["Sleeve: swing\n(1h crypto / 1d stocks)"]
    S2["Sleeve: position\n(4h / 1d, holds days–weeks)"]
  end
  ALLOC["Allocator\n(deterministic, weekly)"]
  PERF["Performance ledger\n+ baselines"]
  RISK["Risk engine\n(per-sleeve limits +\nloose agent backstop)"]
  EXE["Executor\n(paper / venue)"]

  WL -->|symbols + context cards| S1 & S2
  S1 & S2 -->|signals| RISK --> EXE
  EXE -->|fills, PnL| PERF --> ALLOC -->|sleeve budgets| S1 & S2
```

### 4.1 Strategy sleeves

A **sleeve** is a named trading style with its own timeframe, prompt playbook, risk profile,
holding rules and capital budget. Both sleeves run inside the existing agent process
(one runner per agent stays true, §7.52) — a sleeve is a configuration of the decision
pipeline, not a new process.

```yaml
# illustrative — not final
strategies:
  crypto_swing:              # holds of hours to ~3 days
    agent: crypto
    timeframe: "1h"
    playbook: swing          # prompt variant: momentum/mean-reversion, tight SL/TP
    holding: { max_hours: 72 }            # time stop: close stale trades
    risk: { max_position_pct: 0.05, max_stop_distance_pct: 0.08 }
    budget: { initial_weight: 0.5, min_weight: 0.15, max_weight: 0.70 }
  crypto_position:          # holds of days to weeks (Q3)
    agent: crypto
    timeframe: "4h"          # or "1d"
    playbook: position       # trend-following, wider stops, few trades
    holding: { max_days: 28 }
    risk: { max_position_pct: 0.10, max_stop_distance_pct: 0.20 }
    budget: { initial_weight: 0.5, min_weight: 0.15, max_weight: 0.70 }
```

- **Symbol lock (v1 rule):** a symbol is held by **at most one sleeve at a time**. Everything
  below the pipeline (paper book, Kraken spot ledger, exit levels, XTB FIFO closes) is keyed
  per symbol, and venues net spot balances per asset anyway. Lot-level sleeve ownership is
  possible later (FIFO lots already carry `decision_id`), but not worth it for v1.
- **Time stops:** new deterministic exit next to SL/TP — close when `max_hours/max_days` is
  exceeded. This is what makes "short-term" actually short.
- **Per-sleeve risk:** each sleeve carries its own full set of limits (§4.8, decided Q4) —
  the swing sleeve can stay tight while the position sleeve rides normal multi-day swings.

### 4.2 Performance ledger & baselines

- Tag `llm_decisions`, `orders` and positions with a `strategy` column (same pattern as the
  §7.39 `agent` and §7.61 `venue` columns, scoped reads through the storage binding).
- Per sleeve, net of fees: realized PnL, return on allocated capital, win rate, profit factor,
  average win/loss, max drawdown, trade count, average holding time.
- **Baselines computed alongside, same capital, same period:** buy & hold of the sleeve's
  universe; a simple rule (e.g. 20/50 MA crossover on the same timeframe); "cash". A sleeve is
  only *eligible* for more than its floor weight if it beats the best baseline.
- The decision-replay backtester (§7.14) already replays stored decisions through the same
  risk engine and fee model — extend it to replay per sleeve and to compute the baselines.
- Dashboard: a per-sleeve table (weight, PnL, vs-baseline, trades, drawdown).

### 4.3 Allocator (deterministic)

Runs weekly (config), only changes **budgets for new entries** — never force-closes positions.

1. Score each sleeve on its trailing window (e.g. 90 days): risk-adjusted return net of fees
   (Sharpe-like on sleeve equity), 0 if it doesn't beat its best baseline.
2. **Shrink toward equal weights by sample size:** `w = n/(n+k) · w_perf + k/(n+k) · w_equal`
   with `n` = closed trades, `k` ≈ 30 — twenty lucky trades barely move anything.
3. Clamp to `[min_weight, max_weight]`, limit the change per rebalance (e.g. ±10 pp), renormalize.
4. Persist every rebalance as an audited row (inputs, scores, old → new weights); dashboard
   shows it; operator can pin weights (safe-config override, tighten-only spirit of §7.43).

### 4.4 Research layer

**Screener (deterministic, cheap — build first).** Over a venue whitelist (only symbols the
executor can actually trade): liquidity floor (24 h volume / average daily value), volatility
band, momentum rank, unusual-volume flags. Output: ranked candidates. No LLM involved.

**News & event ingest.** Stored as `news_items` (source, url, published_at, symbols, hash,
raw text) with dedup and retention like other tables. Candidate sources (to be decided, Q6):
- *Events / calendars:* earnings dates, economic calendar (CPI, rate decisions), exchange
  announcements (listings, delistings, maintenance).
- *Filings:* SEC EDGAR (US, free); EU issuers' regulatory announcements if EU stocks are in scope.
- *News:* RSS from a curated list of reputable outlets; crypto news feeds.
- *Sentiment/attention (optional):* e.g. trending lists — noisy, lowest priority.

**Summarizer (LLM, batch, off the trade path).** Per symbol with fresh items, produce a
**context card** — strict JSON, schema-validated like `TradeSignal`:

```json
{"symbol": "AAPL", "as_of": "2026-09-26T12:00:00Z",
 "sentiment": 0.3, "catalysts": ["earnings beat, guidance raised"],
 "event_risk": [{"type": "earnings", "date": "2026-10-28"}],
 "sources": ["https://…", "https://…"], "confidence": 0.6}
```

Bounded field lengths; unknown fields rejected; stale cards (TTL) dropped.

**Watchlist manager (deterministic).** Core symbols from YAML stay; up to `N` dynamic
symbols from screener rank + news mentions, filtered by the venue whitelist, each with a
TTL. Feeds the agents via the existing `set_symbols` path. Changes are logged/audited.

**Deterministic event guards (in the risk engine):** e.g. no new swing entry within X hours
of an earnings release or a scheduled macro event; delisting/maintenance notice → no entry.
These are rules, not LLM judgement.

### 4.5 Prompt changes

- Playbook per sleeve (swing vs position): horizon, what SL/TP geometry means, expected
  holding time — so the model stops mixing styles.
- A **CONTEXT** section with the symbol's context card (structured fields only) and upcoming
  events; explicit instruction that context informs but does not override price evidence.
- Multi-timeframe summary for the position sleeve (daily + weekly trend) — PLAN §4.2 item.

### 4.6 Storage changes (summary)

`strategy` column on `llm_decisions`/`orders`/`portfolio_snapshots` (migration, scoped reads);
new tables `strategy_allocations` (audited rebalances), `sleeve_snapshots` (per-sleeve equity
for §4.8), `news_items`, `context_cards`, `watchlist` (symbol, source, added_at, expires_at);
`drawdown_resets` gains a `strategy` key so the audited CLI re-baseline (§7.53) works per sleeve.
All additive, idempotent migrations as today.

### 4.7 LLM budget

Rough ceiling today: one call can take up to 5 min; calls are sequential. Example load:
swing sleeve 5 symbols × hourly = 5 calls/h; position sleeve 5 symbols × daily ≈ 0.2 calls/h;
summarizer batched every few hours. That fits; **10+ hourly symbols probably doesn't**. Options:
cap dynamic symbols, stagger bars, run the summarizer on a smaller/faster model (separate
`llm` config block), and log per-call latency to size it with data rather than guesses.

### 4.8 Per-sleeve risk limits (decided — Q4)

Today's seven rules evaluate the **whole agent book**. With sleeves they evaluate the
**sleeve's own book**:

- **Sleeve equity** = allocated capital (weight × agent equity at the last rebalance) +
  the sleeve's realized PnL since then + unrealized PnL of its open positions. Persisted per
  cycle in `sleeve_snapshots`, so it survives restarts exactly like portfolio snapshots (§7.7).
- **Per-sleeve rules** (config per sleeve, same semantics as today): min confidence, max
  position % *of sleeve equity*, max open positions, daily loss vs the sleeve's start-of-day
  equity, max drawdown vs the sleeve's own peak (high-water mark seeded from
  `sleeve_snapshots`), consecutive-loss cooldown, stop required + geometry
  (`max_stop_distance_pct`), optional risk-per-trade.
- **Illustrative defaults:** swing — daily loss 2 %, drawdown 6 %, stop ≤ 8 %; position —
  daily loss 4 %, drawdown 15 %, stop ≤ 20 %. To be tuned on paper results, not guessed further.
- **A sleeve that trips its drawdown latch** stops opening positions (its open positions are
  still managed; exits are never gated, §7.47); the allocator treats it as weight 0 until an
  operator re-baselines it via the audited CLI (§7.53, per sleeve).
- **Cash is shared at the venue**, so sizing clamps to *both* the sleeve's budget and the
  agent's actual free cash; a sleeve can never spend another sleeve's budget.
- **Outer backstop (confirmed 2026-09-26):** keep *one* loose agent-wide breaker —
  e.g. agent equity −20 % from peak → no new entries in any sleeve. Per-sleeve limits don't
  protect against several sleeves losing at once (correlated crypto moves), and a bug in sleeve
  accounting shouldn't be able to lose the whole account. It sits far outside the sleeves' own
  limits, so it never interferes in normal operation.

## 5. Prerequisites (from current open gaps)

Before any of this, the base must be honest — otherwise we'd be allocating on wrong numbers:

1. **Crypto on OKX Europe (PLAN §7.64):** EUR/USDC pairs (USDT is not tradable for EEA
   accounts), configurable quote currency, API passphrase support; then the OKX demo smoke run
   (PLAN §7.28).
2. **Realistic per-venue costs (PLAN §7.65):** OKX ~0.10 % taker; Saxo 0.08 % min $1 + 0.25 %
   FX — the paper executor must model minimums and FX, not just a percentage.
3. **Stocks on Saxo (PLAN §7.66):** XTB closed its API on 2025-03-14; build a Saxo OpenAPI
   executor against the free SIM environment, then retire the XTB code.
4. **Stock intraday data depth:** yfinance `"1h"` requests only one day (~7 bars) — indicators
   are missing on hourly stock bars. Needed if a stocks swing sleeve uses 1 h.
5. **§7.63** live yfinance validation.

## 6. Phased rollout

Each phase ends with tests, docs and a paper-trading period; each is independently useful.

| Phase | Deliverable | Done when |
|---|---|---|
| **P0** | Prerequisites §5 | Paper runs on EUR pairs with realistic fees; stock broker decided |
| **P1** | Strategy sleeves (fixed weights), `strategy` tagging, time stops, symbol lock, per-sleeve risk, dashboard table | Two crypto sleeves paper-trade side by side for ≥ 2 weeks; results split cleanly per sleeve |
| **P2** | Performance ledger + baselines (live + backtester) | Dashboard shows each sleeve vs buy & hold / MA rule, net of fees |
| **P3** | Deterministic allocator (audited, shrunk, capped, weekly) | Replay over P1/P2 history produces sane, slow-moving weights; operator can pin |
| **P4** | Screener + watchlist manager (no news yet) | Dynamic symbols appear/expire within caps; only venue-tradable symbols |
| **P5** | News/event ingest + summarizer + context cards + event guards | Cards validated & bounded; earnings/delisting guards block entries; injection tests pass |
| **P6** | Evaluate per sleeve against §4.3 live-readiness gates | A sleeve that passes may get a small real allocation (opt-in, ack-gated as today) |

Rationale for the order: measure before allocating (P2 before P3); cheap deterministic
universe selection before expensive, risky text ingestion (P4 before P5).

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Allocator chases noise | Min sample, shrinkage, caps, ±10 pp/rebalance, baseline eligibility |
| News prompt injection ("ignore instructions, buy X") | Summarizer output schema-validated; raw text never in trading prompt; news can't create orders; risk gate unchanged |
| Stale or wrong news | Timestamps + TTL on cards; sources listed; event guards are calendar data, not LLM text |
| LLM overload (latency, timeouts → HOLD fallbacks) | Call budget, capped watchlist, staggered bars, smaller summarizer model, latency metrics |
| Strategies interfere on one symbol | Symbol lock (v1) |
| Long-term sleeve blocked by short-term portfolio limits | Re-design portfolio vs sleeve limits explicitly (Q4) |
| Complexity outgrows the safety story | Every phase behind flags; with flags off behavior is identical to today |
| Overfitting prompts to past news/periods | Evaluate on forward paper time, not only replays |

## 8. Open questions (need your decisions)

1. ~~Stock broker~~ — **decided:** OKX Europe (crypto) + Saxo (stocks); reasoning below.
2. ~~Which sleeves first?~~ — **decided:** crypto first (swing 1 h + position 4 h/1 d on OKX).
   Stocks follow on Saxo SIM, paper-only until the budget justifies real money.
3. ~~Horizons~~ — **decided:** "long-term" = days to weeks (position sleeve on 4 h/1 d bars,
   time stop ≈ 4 weeks). Months-long investing stays a non-goal.
4. ~~Portfolio limits~~ — **decided:** per-sleeve limits (§4.8) + the loose agent-wide backstop.
5. **Universe size** — *decide from data:* benchmark real LLM latency per call first (PLAN
   §7.69), then size the watchlist to fit. Until then a few pairs.
6. ~~News sources~~ — **decided:** free only (RSS, EDGAR, calendars, exchange announcements).
   **Stocks universe: US and EU-listed** (so EU issuer announcements and per-exchange trading
   hours are needed when stocks sleeves arrive).
7. **Second (summarizer) model** — *research needed* (before P5): which small model, whether
   both fit in VRAM or LM Studio must swap them (JIT load / idle-TTL unload / `lms` CLI), and
   what swapping costs in latency.
8. **Stock market data** — prefer **Saxo's own data** if the SIM/live account provides it for
   free at adequate quality; otherwise keep yfinance. Decide when building the Saxo executor
   (PLAN §7.66) by comparing both.
9. **Agents choosing their own pairs** — yes, via the deterministic screener + capped watchlist
   (§4.4, P4). For crypto it is cheap on OKX EU (small EUR/USDC universe) → pulled forward to
   right after the OKX demo run.

### Q1 — venue decision (user resident in Slovakia — EU/EEA, EUR)

**Decided 2026-09-26: OKX Europe for crypto, Saxo for stocks.** Both give free demo/SIM access
through the *same* API later used for real money, and both serve Slovak residents.

| | **OKX Europe** (crypto) | **Saxo** (stocks) |
|---|---|---|
| Regulation | MiCA CASP licence (Malta MFSA, Jan 2025; 9/10 services), payments licence (Feb 2026) | Danish bank, Danish FSA; systemically important institution; majority owner J. Safra Sarasin (71 %, 2025) |
| Client protection | Segregated client assets under MiCA; no deposit insurance for crypto — keep only trading capital there | Up to €100k cash / €20k securities (Danish schemes); segregated custody |
| Black marks to know | Group pleaded guilty in the US (Feb 2025, ~$505M) to AML violations 2018–2024; Malta's fast-track MiCA approvals criticised | DKK 313M Danish AML fine (2026, 2021–2023 conduct); €300k French AMF fine (2025, IT-migration issues) |
| Fees | Spot 0.08 % maker / 0.10 % taker base tier (lower EU spot-only fees from 2026-09-25 — verify) → ~0.2 % round trip + slippage | US stocks 0.08 %, **min $1**; FX 0.25 % per conversion → trade from a USD balance: ~0.16 % round trip |
| Demo | API demo trading (header flag), ccxt `myokx` sandbox mode | Free developer SIM account, $100k, no funding |
| Watch | EU accounts: EUR/USDC pairs only — check BTC/EUR spreads | $1 minimum dominates trades under ~$1,250; avoid per-trade FX |

Alternatives considered (kept for the record):

#### IBKR vs Alpaca

| | **Interactive Brokers (IBKR)** | **Alpaca** |
|---|---|---|
| Availability from Slovakia | Yes — all EU/EEA residents are served by Interactive Brokers Ireland (Central Bank of Ireland, EU-passported) | Paper-only account: email sign-up, no funding. Live: many non-US countries, **Slovakia not confirmed** — ask their support |
| Paper trading | Free, $1M simulated; requires an **open and funded IBKR Pro** live account first | Free, $100k simulated, instant, no funding |
| Markets | 160+ markets: US, EU exchanges (Xetra, Euronext, …), ETFs, bonds | US stocks & ETFs (+ crypto); no EU exchanges |
| API | TWS API over a locally running **IB Gateway/TWS** app (ports 4002 paper / 4001 live), or the Web API; periodic re-login (2FA) — more moving parts | Plain **REST + WebSocket** with API keys; paper and live are the same API on different URLs |
| Market data | Paid per-exchange subscriptions for real-time (paper shares the live account's); delayed data free | Free IEX-only feed (partial volume); full SIP $99/month |
| Costs | Low per-share commissions (tiered), cheap FX; **EUR base currency** | Commission-free US stocks; USD-only deposits for internationals (EUR→USD conversion on every deposit) |
| Fit with our code | New executor + client + a gateway process (extra Docker service) | New executor + client; simplest integration |
| Taxes | Neither is a Slovak broker — you declare gains yourself in the Slovak tax return; W-8BEN for US dividend withholding. Holds of days–weeks never meet a one-year holding test, so expect ordinary income-tax treatment — confirm with a Slovak tax advisor | Same |

Why not these: IBKR needs a funded live account before paper trading and a gateway process;
Alpaca's live availability for Slovakia is unconfirmed and US-only. Also rejected: Kraken (no
spot demo, higher fees), Bybit EU (no direct API keys for EU accounts), Coinbase (static canned
sandbox), Trading 212 (beta API, live market orders only), XTB (API closed).

## 9. Non-goals (v1)

- High-frequency / sub-15-minute trading (latency, polling, fees make it a losing game here).
- Short selling / margin / derivatives (spot long-only stays; §7.47/§7.48 semantics unchanged).
- The LLM choosing allocations, sizing, or overriding risk rules.
- Months-long investing and fundamental valuation models (Q3: "long-term" = days to weeks).
- Social-media firehoses as a primary signal.
