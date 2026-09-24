# External Code Review 4 — Autonomous Trading Agent

**Date:** 2026-09-24
**Commit reviewed:** `811c4ea` (main, clean tree)
**Scope:** everything under `src/`, plus `scripts/`, `config/settings.yaml`, `docker-compose.yml`, the docs set (`AGENTS.md`, `ARCHITECTURE.md`, `PLAN.md`, `HISTORY.md`, `nightly_finds.md`, earlier reviews) and the local `data/trading_agent.db`, which I opened read-only for evidence.
**Method:** I read all of the code by hand. I ran throwaway probe scripts (not committed) against the real classes to confirm the most serious findings, and ran ccxt 4.5.78 to check what Kraken's sandbox actually supports. I did not change any repo files except adding this one.

---

## 1. Verification status

| Check | Result |
|---|---|
| `pytest` | **553 passed** in 7.0 s, **94 %** line coverage on `src/`, 0 warnings |
| `ruff check .` | clean |
| `ruff format --check .` | 94 files already formatted |
| CI | **none** (no `.github/`, no other pipeline). The test counts above were only ever run locally, and the local venv is **Python 3.14** while the Docker image is **3.11**. |

The engineering discipline is high. The code is consistently fail-soft, uses Protocol seams everywhere, documents its design decisions, and the §7 history reads like a good change log. **Most of the findings below are in places the unit tests can't reach:** two processes sharing one database, the real behaviour of each venue's API, and feedback loops that only appear over many cycles. Each component is tested on its own, but several of the system's promises break when the real components are put together.

### What the stored data says

`data/trading_agent.db` holds 108 decisions (2026-09-13 → 2026-09-21). **All of them are HOLD, no order has ever been placed, and 60 of the 108 (56 %) are LLM fallbacks** (`"All connection attempts failed"`). All 60 snapshots are flat at 100 000. So the live-readiness clock in PLAN §4.3 ("paper PnL positive for ≥ 4 weeks") has not really started, and none of the trading paths below have ever run for real.

---

## 2. Critical / high severity

### C1. Two agents share one database, and no persisted table records which agent a row belongs to
- **Where:** `src/core/storage/models.py:85-93` (`PortfolioSnapshotRow`, no agent column). The same applies to `llm_decisions` and `orders`. Consumers: `core/rehydration.py:433-458`, `core/runner.py:112`, `storage/snapshots.py:454-490`, `dashboard/app.py:229-259`, `core/control_api.py:88,170`.
- **Problem:** `docker-compose.yml` runs `agent-crypto` and `agent-stocks` against the same `agent-data` volume and `storage.database_path`. Only `agent_control` is keyed per agent. When both agents are enabled:
  1. **Restart rehydration loads the wrong book.** `get_latest_portfolio_snapshot()` returns whichever agent wrote last. The crypto `PaperExecutor` then adopts the stocks agent's cash and AAPL positions, or the other way round. Filled-order replay (`get_filled_orders()`) is unfiltered too.
  2. **The drawdown high-water mark is shared.** `get_max_portfolio_value()` is a MAX over both agents. If one agent grows by more than 5 %, the other is **permanently** blocked from new entries.
  3. The daily-loss baseline (the first snapshot of the day) and the loss-streak rehydration (`get_closed_decisions`) mix both agents in the same way.
  4. The dashboard's portfolio chart and positions panel, and the control API's `/portfolio`, jump between the two agents' books.
- **Proof (probe script against the real classes):** crypto snapshot at 100k, then stocks snapshot at 110k, then crypto restart:
  ```
  crypto executor after restart: 50000.0 ['AAPL']
  crypto (equity 100k) entry verdict: Drawdown 9.09% exceeds limit 5.00% (peak equity 110000.00)
  ```
- **Why tests miss it:** every test builds a single agent per DB.
- **Fix:** add an `agent` column to `portfolio_snapshots`, `llm_decisions` and `orders` (migration default `'crypto'` for existing rows), and filter every read by `component`. The alternative is one DB file per agent, but that breaks the dashboard's single-DB design. Pin it with an integration test that runs both runners against one DB.

### C2. XTB: every SELL opens a new position instead of closing the long
- **Where:** `src/execution/xtb_client.py:49-52` and `:250-272`.
- **Problem:** `create_order` always sends `tradeTransaction` with `type=_TYPE_OPEN` (0). In xAPI, `cmd=SELL, type=OPEN` **opens a sell (short) position**. Closing an existing trade needs `type=CLOSE` (2) plus the position's `order` number. `_TYPE_CLOSE` doesn't exist anywhere in the module. As a result, on the XTB demo path:
  - LLM sells, stop-loss/take-profit auto-exits (§7.9) and close-all (§7.15) all **open shorts** and leave the long in place. The account ends up hedged, with twice the margin.
  - Meanwhile the local FIFO tracker (`xtb_executor.py:137-140`) books the sell as closing the long and reports a realized PnL that never happened. That fake outcome feeds the loss streak and the LLM's "learn from your track record" prompt.
  - Next cycle, `get_positions()` reports the new short. `_check_exit_levels` and `close_all_positions` pick positions by `quantity > 0` and ignore `side`, so the next close-all **SELLs the short, opening yet another one** (see H6).
- **Fix:** keep a map from symbol to open xAPI trade `order` ids (from `getTrades`). Route SELLs on a held long to `type=CLOSE` against those orders, splitting volume if needed. Refuse opening shorts unless a future margin mode asks for them explicitly. Add a transport-fake test that asserts the CLOSE payload.

### C3. The keyed Kraken ("testnet") path can't work, and its only workaround trades real money
- **Where:** `src/data/ccxt_provider.py:159-160`, `scripts/run_crypto_agent.py:47-57`, `src/execution/kraken_executor.py:289-304`.
- **Problems:**
  1. **Kraken spot has no ccxt sandbox.** On ccxt 4.5.78, `kraken().urls['test']` is `None`, and `set_sandbox_mode(True)` raises `TypeError: 'NoneType' object is not iterable`. With the shipped `testnet: true`, setting `KRAKEN_API_KEY` crashes the runner at startup with that unclear error. That fails safe, but PLAN §7.28 ("live Kraken testnet smoke pass") and nightly find #1 depend on an endpoint that doesn't exist. Only **Kraken Futures** has a demo environment (`demo-futures.kraken.com`). This is the same kind of mistaken premise as find #13 (XTB "OAuth2").
  2. **The obvious workaround is live trading.** If someone sets `testnet: false` to get past the crash, orders go to **production Kraken with real funds**, while the runner still logs `"using live public data + Kraken testnet executor"` and reports mode `"kraken-testnet"`.
  3. **Even with a working endpoint, the valuation would be wrong.** On spot, `get_positions()` always returns `[]` and `get_cash()` returns only *free* USDT. So `total_value` = free quote balance: every BUY looks like a loss equal to its own notional. One 10 % buy registers as a −10 % day and a 10 % drawdown, which trips both gates immediately. The persisted snapshot then makes that permanent across restarts through the drawdown seed. Stop-loss/take-profit enforcement never fires (there are no positions to check), close-all does nothing, and SELL sizing isn't clamped to holdings. AGENTS.md's statement that "exit levels are enforced deterministically" is false for this executor.
- **Fix:** relabel the keyed path as *live*. Put it behind an explicit opt-in (for example `crypto_agent.live_trading_acknowledged: true` plus an env flag), never behind `testnet: false` alone. Build spot "positions" from the local FIFO ledger plus `fetch_balance()` totals, marked at the snapshot close. If a sandbox is really needed, target `krakenfutures` demo, which means the short/derivatives model from §7.38 becomes load-bearing. Update §7.28 and find #1.

### C4. `max_position_pct` caps each order, not each position, so the LLM can pyramid to ~90 % in one symbol
- **Where:** `src/core/risk_engine.py:254-283`, `src/core/decision_pipeline.py:545-578`.
- **Problem:** the gate compares the *planned order notional* with `max_position_pct × total_value`. Nothing looks at the resulting position size. `_check_max_positions` only blocks *new* symbols. ARCHITECTURE.md's risk table says "10 % of portfolio **per symbol**".
- **This is likely in practice:** the crypto agent asks every **5 minutes** about **1-hour** candles (C6-adjacent, M7), and the prompt never tells the LLM what it already holds (H3). A model that stays bullish will BUY again each cycle.
- **Proof (probe, real `RiskEngine` + `PaperExecutor` with shipped fees):** 12 consecutive approved BUY signals → **9 fills, BTC = 90 % of equity**. It only stopped because fee and slippage make the cash-bound buy fail (see L1).
- **Fix:** in `_check_position_size`, add the existing exposure for `signal.symbol` to the planned notional for BUYs and reject if the total exceeds the cap. Size BUYs as `min(cap − existing, …)`. Add a Hypothesis property test: *for any signal sequence, exposure per symbol ≤ cap*.

---

## 3. Medium-high severity

### H1. The browser can drive the dashboard and control API from any website (CSRF / DNS rebinding), including loosening the risk limits and starting agents
- **Where:** `src/dashboard/app.py:346` (`POST /config/{agent}`), `:390` (`POST /control/...`), `:415` (`POST /launch/...`), `src/core/control_api.py:182-201`. Also `config/settings.yaml:103` ships `allow_launch: true`.
- **Problem:** there's no authentication, no CSRF token, and no `Origin`/`Host` check. The dashboard parses urlencoded bodies itself, so a plain cross-site `<form method=POST action="http://127.0.0.1:8080/config/crypto">` is a "simple request" with no CORS preflight. Any page open in the operator's browser can:
  - set `risk.max_drawdown_pct=1`, `risk.daily_loss_limit_pct=1`, `risk.min_confidence=0`, `risk.max_position_pct=1`, `risk.enforce_exit_levels=false`. All of these pass the "safe" whitelist.
  - trigger close-all or pause, or with `allow_launch: true`, start agents.
  - Binding to loopback doesn't stop this: the request comes from the operator's own browser. DNS rebinding also gets around loopback because `Host` isn't validated.
- **Related policy gap:** the whitelist keeps *credentials* out, but it allows **disabling every risk guard** with a form. That contradicts the rules "risk rules are hard-coded deterministic guards" and "enabling real execution is never a web-form click".
- **Doc drift:** AGENTS.md says `allow_launch` defaults to false and should stay false under compose. The shipped YAML says `true`, and compose bind-mounts that same YAML into the dashboard container.
- **Fix:** reject state-changing requests whose `Origin`/`Referer` isn't the dashboard's own origin. Validate `Host` against an allowlist. Add a per-session CSRF token to the HTMX forms. Allow risk overrides only to *tighten* limits relative to the YAML (for example `override ≤ yaml` for pct limits and `≥` for `min_confidence`), and keep `enforce_exit_levels` out of the web surface. Ship `allow_launch: false`.

### H2. Persistence after an order is placed isn't fail-soft, so one DB error can lose a fill and abort the cycle
- **Where:** `src/agents/base_agent.py:148-161` (`_post_process` runs *outside* the per-symbol `try`), `:283-315`, `:354-382`, and `:269-271` (reconciliation).
- **Problem:** `_persist_order` → `storage.save_order`, `_persist_portfolio` → `save_portfolio_snapshot`, and the `add_realized_pnl` calls in `_reconcile_orders` aren't wrapped. A transient `database is locked` (the dashboard and control API write to the same file) or a disk-full error after `place_order` succeeded:
  - loses the `orders` row, which is the **source for the §7.25 FIFO replay**, so the book and the ledger diverge after the next restart;
  - skips the remaining symbols in the cycle and **skips the heartbeat** (`_record_health` is never reached);
  - in reconciliation, the executor has already dropped the pending order from `_open_orders`, so that status change is gone for good.
- **Fix:** wrap `_post_process` for each symbol like the pipeline call. Make order persistence retry, with the order dumped to the structlog audit line as a last resort. In reconciliation, pop the pending order only after the row has been persisted.

### H3. The prompt never shows the LLM its position or cash, and shows HOLD/rejected rows as "still open"
- **Where:** `src/analysis/prompt_builder.py:15-77`, `_format_outcome` at `:80-92`.
- **Problem:** the user prompt includes candles, indicators and past decisions, but **not whether the agent holds the symbol, its size, entry price, unrealized PnL, the stop/take-profit levels in force, or available cash**. The model can't tell "open a position" from "add to one" (which feeds C4), and it will issue SELLs on symbols it doesn't hold. Also, every HOLD or risk-rejected decision has `realized_pnl = None` forever, so the CONTEXT block shows it as `outcome: still open`. In the real DB, that's 100 % of the history. The model is being told it has ~10 open trades that don't exist.
- **Fix:** add a `POSITION` section built from the executor book (qty, avg entry, mark, uPnL, SL/TP, cash). Render `n/a` for HOLD/rejected rows and keep `still open` for approved entries whose lots haven't closed.

### H4. Restart rehydration counts each losing round trip twice in the loss streak
- **Where:** `src/core/rehydration.py:494-511`, `src/agents/base_agent.py:301-312`.
- **Problem:** live, `record_outcome` runs **once per closing fill**. But `_post_process` writes the realized PnL onto **both** the SELL decision (`set_realized_pnl`) **and** the entry BUY decision (`add_realized_pnl` through `closed_entries`). `get_closed_decisions` then counts *both rows*. After a restart, 2 losing trades look like a streak of 4, which is ≥ 3 and re-arms a cooldown the live process never had. It's also inconsistent with auto-exits and close-all, which carry no `decision_id`, so they write only the entry row. The LLM's context shows the same PnL on both rows as well.
- **Fix:** rebuild the streak from closing fills (sell `orders` joined to their realized PnL), or only count `action='buy'` entry rows. Record the closing order's PnL once.

### H5. The risk gate blocks discretionary exits, even though the design says exits must never be stranded
- **Where:** `src/core/risk_engine.py:168-176`.
- **Problem:** a SELL of a held position runs through confidence, daily-loss, **drawdown** and **cooldown**. Once drawdown exceeds 5 % (a latch that's effectively permanent, see M4), the LLM can **never** close a position. Only SL/TP or a manual close-all can. The pipeline comment at `decision_pipeline.py:218-222` and `_check_stop_loss` both say closes reduce risk and must not be gated. The engine applies that reasoning to the stop-loss rule only.
- **Fix:** when `signal.action == SELL` and the symbol is held long, skip the exposure-increasing rules (daily loss, drawdown, cooldown, max positions). Keep the confidence rule if wanted.

### H6. Code that closes positions ignores `Position.side`, which §7.38 made real
- **Where:** `src/core/decision_pipeline.py:145-160` (`close_all_positions`), `:373` and `:532-542` (`_check_exit_levels` / `exit_level_breach`).
- **Problem:** since §7.38, Kraken and XTB report shorts as `side=SHORT` with positive quantity. Close-all and exit enforcement still close everything with `OrderSide.SELL`, and apply long-only breach logic. On a short, that *increases* exposure, and a short's stop above entry is read as a take-profit. Combined with C2, this is reachable today on XTB.
- **Fix:** close shorts with BUY (cover), mirror `exit_level_breach` for shorts, or skip shorts with a warning while the system is spot-only. Add tests with a `side=SHORT` position.

### H7. The backtester has look-ahead bias because candles are keyed by their open time
- **Where:** `src/core/backtester.py:320-338` (the `_build_timeline` / `_on_candle` pairing).
- **Problem:** ccxt and yfinance timestamps are candle **open** times. The timeline processes a candle at its open timestamp but uses its **close** for marking, exit checks and decision pricing. A decision at 10:00 on a `1d` stock series is filled at that day's 16:30 close. On `1h` crypto, it's filled up to an hour in the future. Stop-loss/take-profit also fire on closes the live system couldn't have seen yet. The replayed PnL is therefore optimistic in a systematic way.
- **Fix:** put candle events at `open_ts + timeframe` (their close time), or price decisions at the last *completed* candle's close. Add a test where a decision falls inside a candle.

---

## 4. Medium severity

### M1. Some "safe config" overrides are silently ignored, the same class of bug as find #16
- `market_hours`: `parse_and_apply` writes `settings.stocks_agent.market_hours`, but `StocksAgent` checks its own copy `self._market_hours`, captured at construction (`stocks_agent.py:142,154`). The override never takes effect.
- `interval_minutes`: this is documented as "applies after restart", but it **never** applies. After a restart the runner schedules from the YAML value (`run_crypto_agent.py:91` → `runner.py:161`), *before* the first cycle applies overrides. The real DB has a stored `interval_minutes: 1` override that has never been honoured.
- **Removing an override doesn't revert anything.** `parse_and_apply` returns early on `None`, so live objects keep the old values until restart.
- **The dashboard form saves every field it displays.** The stored override in the real DB pins *all* risk values to their current defaults, so a later stricter YAML edit is silently overridden.
- **Fix:** give each overridable field one reader (the agent reads `settings.*` every cycle), reschedule the APScheduler job when the interval changes, re-apply the YAML base plus overrides on every change including removal, and have the form submit only fields that differ from the YAML.

### M2. An LLM outage doesn't show as unhealthy anywhere
- **Where:** `src/agents/base_agent.py:155-160`, `:317-350`.
- A fallback HOLD isn't an error, so `last_error` stays `None`, the dashboard shows **running**, and `_maybe_alert` sends nothing. In the real DB, 56 % of decisions are fallbacks and nothing surfaced it. Alerts only go to `NoopAlertSink` (log lines) anyway.
- **Fix:** treat `signal.is_fallback` as a cycle error (for example `last_error="LLM unavailable"`), add a fallback-rate figure to the dashboard, and add at least one real alert sink (email/ntfy/webhook) before the live-readiness phase.

### M3. No single-instance guard, so a second runner for the same agent doubles the trading
- **Where:** `src/core/runner.py:70-135`, `src/dashboard/app.py:421-434`.
- Nothing stops two `run_crypto_agent` processes. Each keeps its own in-memory paper book, and both write decisions and orders into the same DB. The dashboard's twin check relies on a *fresh heartbeat*, which a just-started runner doesn't have yet: it's still in rehydration or waiting on a slow first LLM call. `allow_launch: true` plus the compose-managed agent makes this easy to hit.
- **Fix:** have the runner take an exclusive lock at startup (an `fcntl` lock on `data/<agent>.lock`, or a lease row in `agent_control` with a PID and a TTL).

### M4. The drawdown gate is a permanent latch with no reset path
- `portfolio_snapshots` are never pruned and the peak is `MAX(total_value)` over all time, so once equity falls 5 % below its best-ever value, entries are blocked **forever**. The only way out is recovering above 95 % of the peak, which is hard with entries blocked, or editing the DB by hand. This may be intended, but it's undocumented as an operating procedure, and C1 and C3 can trigger it for spurious reasons.
- **Fix:** add an explicit, audited "re-baseline peak" action (CLI only, not the web UI), and document the procedure.

### M5. Entry stop-loss and take-profit levels are never sanity-checked
- **Where:** `src/core/risk_engine.py:340-349`.
- A BUY only needs `stop_loss is not None`. A stop at or above the current price, or a take-profit at or below it, gets closed by §7.9 on the next cycle, paying fees twice for nothing. A stop at `0.01` gives unlimited risk per trade. Sizing ignores the stop distance.
- **Fix:** reject entries unless `stop_loss < price < take_profit`, and cap `(price − stop_loss)/price` with a config limit. Optionally size by risk per trade (`risk_pct × equity / stop_distance`).

### M6. With no usable price, the notional cap is skipped and a market order can still go out
- **Where:** `src/core/decision_pipeline.py:256-312`, `:558-560`.
- With empty candles, `planned_notional` is `None`, so the cap check is skipped. The quantity is then sized at `stop_loss`, or at **price 1.0** if there's no stop, and sent with `price=None`. The comment says "let the executor reject the market order". `PaperExecutor` does reject it, but `KrakenExecutor` turns `price=None` into a real **market** order (`kraken_executor.py:145`) whose size the gate never checked.
- **Fix:** refuse to trade (and skip the LLM call) when the snapshot has no candles.

### M7. The candle timeframe is hard-coded and mismatched with the cycle interval
- `CryptoAgent(timeframe="1h")` and `StocksAgent(timeframe="1d")` aren't configurable, which breaks the "config-driven behaviour" rule. With `interval_minutes: 5` (crypto) and `15` (stocks), the LLM sees essentially the same candles 12 and ~30 times per candle, including a forming last candle with partial volume. That costs LLM time and, together with C4 and H3, invites repeated entries.
- **Fix:** add `timeframe` to each agent's config block, drop the in-progress candle (or label it clearly as forming), and consider pairing the interval with the timeframe by default.

### M8. The LLM response parser can't handle reasoning models or leading prose
- **Where:** `src/core/llm_client.py:183-200`.
- The configured model is `qwen/qwen3.8-27b`. Qwen3-family models emit `<think>…</think>` blocks unless told not to, and some LM Studio builds put those in `content`. `_parse_signal` only strips code fences and then runs `json.loads` on the whole text, so every attempt fails and the cycle falls back to HOLD. With `max_tokens: 1024`, a long reasoning block can also cut off the JSON.
- **Fix:** strip `<think>` blocks and pull out the last balanced `{…}` object. Turn on `use_json_schema` once the model supports it. Add parser tests with a think-prefixed response.

### M9. The venue executors don't restore FIFO ledgers or pending orders after a restart (find #7 overstates "fixed")
- `rehydrate_paper_executor` only runs for executors with `load_portfolio_state`, which only `PaperExecutor` has. After a restart, `KrakenExecutor`/`XTBExecutor` have empty ledgers and empty `_exit_levels`, so stops on pre-restart positions aren't enforced, and their closes report no outcome. `KrakenExecutor._open_orders` is also in memory only: an order left `open` across a restart stays `pending` in the DB forever. `nightly_finds.md` #7 and AGENTS.md describe §7.25 as rebuilding "the FIFO ledgers" in general.
- **Fix:** give venue executors a `load_fills()` hook fed from `get_filled_orders(symbol in executor's symbols)`, persist exit levels with the order or decision, and reload `status='pending'` order rows into `_open_orders` at startup.

---

## 5. Low severity and nits

- **L1. Sizing ignores fees and slippage.** Paper fills at `price × (1 + slippage)` plus a fee, so whenever cash is the limit (`quantity = cash/price`), the executor rejects the buy with "Insufficient cash" *after* the risk gate approved it. Size against `price × (1 + slippage) × (1 + fee)`.
- **L2. Rounding can push a sell quantity above holdings.** `round(quantity, 8)` after clamping to `held` can round a fractional amount up past what's held, and paper rejects `pos.quantity < quantity`. Round down (`math.floor(q·1e8)/1e8`) instead.
- **L3. The first symbol after UTC midnight uses yesterday's baseline.** `_check_daily_loss` only seeds the baseline when it's `None`. The day rollover happens in `update_daily_value`, which runs in post-processing, so the first risk check of a new day compares against yesterday. Call `reset_if_new_day` at the top of `_check_daily_loss`.
- **L4. Close-all and reconciled fills don't feed the loss streak.** Only pipeline fills and auto-exits call `record_outcome`, so a losing close-all or a late venue fill never counts.
- **L5. Formatting assumes large prices.** `Current price: {:.2f}`, `atr_14` and the Bollinger values are rounded to 2 dp. Any sub-$1 asset shows the LLM `0.00`-level numbers. Use significant-figure formatting.
- **L6. Compose leftovers.**
  - `agent-stocks` is `restart: unless-stopped`, but the shipped config has `stocks_agent.enabled: false`. The runner exits 0 by design, and Docker restarts it over and over.
  - Compose passes `XTB_API_KEY`, but the runner reads `XTB_ACCOUNT_ID`/`XTB_ACCOUNT_PASSWORD`, so XTB execution can never be enabled under compose.
- **L7. No live-account guard for XTB.** `xtb_execution.account_type: real` passes validation. A one-word YAML edit plus credentials points the agent at a real-money account. It should need the same explicit acknowledgement as C3.
- **L8. There's no symbol mapping between data and execution.** yfinance's `AAPL` isn't an xAPI symbol; XTB uses suffixed names like `AAPL.US`. The mapping should be a config table.
- **L9. `_REQUEST_STATUS_MAP` has codes the API doesn't define.** It maps xAPI `requestStatus` 2, 5 and 6, which I believe don't exist (the documented set is 0 error, 1 pending, 3 accepted, 4 rejected). It's harmless, but it's a sign the enum wasn't checked against the spec, which also applies to C2.
- **L10. Doc drift.**
  - AGENTS.md says the pidfile is `data/<agent>.pid`; the code writes `data/<agent>_agent.pid`.
  - ARCHITECTURE.md repeats the "Hard-coded, non-negotiable gates…" sentence twice (risk-engine section).
  - The risk table says "per symbol" (see C4).
- **L11. Leftover state.**
  - The real DB still has an orphaned `agent_control` row `crypto_agent` from before the find #16 fix.
  - An untracked `build/lib/` holds a stale copy of the pre-§7.36 code (for example `build/lib/src/core/storage.py`), which confuses grep-based tools and coding agents. `rm -rf build/`.
- **L12. Tests only run on one Python version.** Tests run on 3.14 locally and the image is 3.11. Add a minimal CI job (ruff + pytest on 3.11, the Docker target) so the "N tests passing" claims in the docs are reproducible.

---

## 6. What's working well

- **Clear safety design.** There's one lifecycle (`run_agent`), one agent base class, one sizing and exit function shared by live and replay, and an `enabled: false` path that really does nothing. Fail-soft behaviour is consistent, and the control-plane checks use strict `is True`.
- **Honest outcome accounting.** The FIFO ledger attributes results to the entry decision, reports no outcome rather than a made-up one, keeps fallback HOLDs out of the prompt with an unforgeable `is_fallback`, and logs the full prompt and response for audit.
- **Testability.** Every venue sits behind a Protocol, and the risk engine takes an injectable clock. There are Hypothesis tests for the risk invariants and 553 fast tests.
- **Documentation culture.** The numbered §7 items, nightly finds, and a HISTORY file that records *why* each change was made are unusually good. Several findings here (C3 and M9) are mainly places where that documentation claims more than the code does.

---

## 7. Recommended order of work

1. **C1: scope storage per agent.** Nothing else about restart safety or the drawdown gate holds while two agents share an unscoped DB.
2. **C4 + H3 + M7: cap exposure per position, show the LLM its book, fix the timeframe.** These three together decide whether the first real paper trades are meaningful or just pyramiding.
3. **H1: CSRF/Origin protection and tighten-only risk overrides.** Ship `allow_launch: false`.
4. **H2, H4, H5, M2: make post-order persistence fail-soft, fix the double-counted streak, don't gate exits, surface LLM outages.** Then run paper trading and start the §4.3 clock. Fix M8 first if the Qwen model is still in use.
5. **H7: remove look-ahead from the backtester** before using any replay numbers for the §4.3 gates.
6. **Before any venue work:** C2 (XTB close semantics), C3 (Kraken "testnet" premise and a live-trading acknowledgement), H6 (side-aware closes), M9 (venue rehydration). Update PLAN §7.28 so it no longer assumes a Kraken spot sandbox exists.
7. **Add CI (L12)** so later reviews aren't the only integration test.
