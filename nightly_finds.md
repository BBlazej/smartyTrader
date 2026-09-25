# Nightly Finds — bugs & gaps discovered during the §7 implementation run

Discovered while implementing `PLAN.md` §7 (Gaps & Next Steps), starting 2026-09-16.
Each entry: where it was seen, what's wrong, and current status. Nothing here is
fixed unless explicitly marked.

## Environment / verification limits

1. **Live Kraken testnet pass cannot run in the dev sandbox** (seen in §7.6).
   Outbound HTTPS to `api.kraken.com` is blocked here, so the keyed-path fixes
   (nested-balance parsing, closed-order fill recording, spot `fetch_positions`
   degradation) are verified against recorded-shape fixtures only. A real
   testnet smoke run (`KRAKEN_API_KEY=... python -m scripts.run_crypto_agent --once`)
   is still outstanding before "Kraken testnet mode" is offered.
   **Status: re-scoped by §7.41 — the premise was wrong.** Kraken spot has *no*
   ccxt sandbox at all, so a "Kraken testnet" run can never exist; the smoke pass
   is now either a keyed run on a sandbox-having exchange or a deliberately
   acknowledged minimal-size live run (PLAN §7 item 28). The keyed path itself is
   no longer offered accidentally.

2. **`yfinance` is not installed in the dev venv**, so the stocks provider's real
   network path (and the §7.11 data-depth fix) can only be exercised through the
   injected-source seam; no live yfinance validation from this sandbox. **Status: open.**

## Code nits noticed en route (candidates for §7.19)

3. **Daily-loss boundary is float-fragile** (`risk_engine.py`, seen while adding
   hypothesis tests): the rule fires at `pnl_pct <= -limit` exactly; whether a
   drop of *exactly* `-2.000…%` trips it depends on float rounding of the specific
   values. Not wrong, but tests asserting the exact boundary are brittle (one
   property test had to widen its range). Consider `>=` with a small epsilon or
   documenting "strictly beyond". **Status: fixed in §7.37** — the comparison now
   carries a relative epsilon biased toward rejection (rounding can never buy an
   approval at the cap), pinned by `TestDailyLossEpsilon`.

4. **Position side is ignored in the shared model** (`core/models.py::Position`,
   seen in §7.6): `KrakenExecutor.get_positions` maps *short* ccxt positions into
   `Position` with positive quantity, i.e. a short looks like a long to the risk
   engine and valuation. The system is spot-only today so it never bites, but any
   future margin/derivatives work must address this first. **Status: open (by design for now).**

## Review-document drift

5. `review.MD` claimed "no `.env.example` exists" — stale: the repo has had one at
   the root since the init commit; what actually existed was a *divergent duplicate*
   at `config/.env.example`. Resolved in §7.4 by consolidating into the root file.

## Found while implementing §7.8 (outcome attribution)

6. **The stop-loss gate rejects exit orders that lack a stop** (`risk_engine.py::_check_stop_loss`):
   `Action.SELL` is treated like an entry, so a closing order without `stop_loss`
   is rejected ("Active trade signal must include a stop-loss") and the position
   can't be closed by the LLM at all. Meaningless for an exit — the stop belongs to
   the position, not the close. Integration tests had to pass dummy stops to get a
   sell through. §7.9 (deterministic SL/TP enforcement) should exempt closes from
   this rule while keeping it for entries. **Status: resolved in §7.9** — the rule
   now applies to entries only, and exits bypass the gate entirely.

7. **Venue FIFO ledgers are memory-only across restarts** (`kraken_executor.py`,
   `xtb_executor.py`, `paper_executor.load_portfolio_state`): the local lot ledger
   is rebuilt only approximately — paper from the avg entry price of the loaded
   snapshot (one merged lot, no decision ids), Kraken/XTB not at all. Consequence:
   a position opened before a restart closes afterwards with *no* realized-PnL
   attribution (we deliberately report nothing rather than a fabricated number).
   Fix would be replaying `orders` rows (buy fills carry `decision_id`, price and
   quantity) into the tracker at startup. **Status: fixed in §7.25 (paper) and §7.58
   (Kraken/XTB)** — filled-order replay rebuilds the FIFO ledgers with entry-decision ids
   across restarts; venue executors also get their exit levels and pending orders back.

8. **XTB tracking only works for priced orders** (`xtb_executor.py`): the xAPI seam's
   `create_order` payload is mapped without a fill price, so a *market* order that
   fills has no basis to record and its later close reports no outcome. The pipeline
   always sends marketable limits, so it does not bite today; real xAPI work (§7.16)
   should read fills from the venue's order/position stream instead. **Status: open**
   (§7.16 has since landed — instant orders + `tradeTransactionStatus` polling — but
   the executor still books fills at the requested price, not the venue fill price).

9. **`closed_entries` assumes a single-sided (long-only) book**: the tracker consumes
   lots on sells only, matching this system's spot-only usage. If shorts are ever
   supported (see #4), `PositionTracker.on_sell` must also handle opening shorts and
   buys closing them. **Status: fixed in §7.38** — explicit `open_short()`/`cover()`
   ledgers with the same FIFO attribution; sides are chosen by executors, never
   inferred from order verbs.

## Found while implementing §7.14 (decision-replay backtesting)

10. **Risk-engine trackers are wall-clock-bound during replay** (`risk_engine.py`
    via `backtester.py`): `DailyLossTracker` and the losing-streak cooldown derive
    "today" from `datetime.now(UTC)`, so a replay of months of decisions behaves as
    one continuous day — the daily-loss cap becomes a whole-window cap, and
    cooldowns are relative to replay execution rather than each decision's own
    date. Live behavior is correct; only replay fidelity is affected. A fix means
    injecting a clock into `RiskEngine` (and updating its many tests); for now the
    limitation is documented in the backtester module docstring and CLI output.
    **Status: fixed in §7.27** — `RiskEngine` takes an injectable `Clock`; the
    backtester drives it from candle/decision timestamps via `TimelineClock`.

11. **Sharpe annualization is coarse for non-24/7 series** (`backtester.py`):
    equity-curve returns are scaled by √(timeframe's nominal periods/year), which
    ignores weekends/holiday gaps in stock candles — stock Sharpes are overstated
    relative to crypto. Acceptable for v1 comparison; revisit with calendar-aware
    period counts if the stocks backtest becomes important. **Status: fixed in
    §7.37** — `estimate_periods_per_year` infers realized cadence from the equity
    curve itself (daily stock series → ~252), falling back to the nominal table
    for short/implausible curves; MACD also optimized O(N²)→O(N) alongside it.

12. **`YFinanceSource.fetch_history` dropped the interval argument** (found by
    lint while writing §7.14 tests): `_fetch_range` was called without the mapped
    yfinance interval, so an hourly-window backtest would have silently fetched
    daily bars. **Status: fixed in §7.14** — interval is passed through and pinned
    by `TestFetchHistoryRange.test_yfinance_source_filters_to_window`.

## Found while implementing §7.16 (XTB demo execution via xAPI)

13. **PLAN §7.16's "OAuth2 flow" premise was outdated, and the API hosts moved**
    (`xtb_client.py`): XTB retired `ws.xtb.com`/`xapi.xtb.com` on 2025-03-14; xAPI now
    runs on `wss://ws.xapi.pro/{demo,real}` with its classic `login` command (account id +
    the xStation-generated xAPI verification code) — there is **no OAuth2 token endpoint**
    anywhere in the protocol. The canonical docs domain (xapi.pl) is dead and every older
    wrapper library carries a deprecation notice, so implementers must ground against
    maintained wrappers rather than the original spec. Related accepted limitations pinned
    in §7.16 code/docs: xAPI sizes positions in *lots* (≈1 share per lot for XTB equities,
    symbol specs not validated), `create_order` payloads carry no commission (fills tracked
    gross — §7.8 precedent), and position marks come from one-shot `getTickPrices`, not the
    streaming channel. **Status: implemented accordingly (§7.16); the stale
    `xtb_executor.py` docstring residue was corrected in §7.30.**

## Found while using the dashboard (post-§7.19)

14. **Dashboard showed stopped agents as "running"** (`src/dashboard/`): the health badge
    rendered the raw `agent_control.state` latch, which records *intent* only — it keeps its
    last value (`running`, or the default when no row exists) forever after the process dies,
    and nothing checked the heartbeat. Now the badge shows an effective status derived in
    `views.py::agent_status`: `disabled` → `paused` (latch) → `offline` when `last_cycle_at`
    is missing or older than 2× the agent's `interval_minutes` (floored at 10 min, +5 min
    grace) → else `running`. Related gap on the agent side: skipped cycles (market-hours
    guard) returned before stamping the heartbeat, so a *live* stocks agent would have
    false-alarmed offline overnight — `BaseTradingAgent.run_cycle` now records a heartbeat
    on skip too. Pause returns earlier and needs none (the latch itself renders `paused`).
    **Status: fixed.**

## Found during external review 3 (2026-09-21)

15. **Hardcoded consecutive-loss threshold in state rehydration** (`src/core/rehydration.py:98`):
    `rehydrate_risk_engine` checks `streak >= 3` instead of `risk_engine.settings.consecutive_losses_threshold`.
    If configured to a value other than 3 in `settings.yaml`, restart rehydration misapplies cooldown evaluation.
    **Status: fixed in §7.26** — the threshold is read from `risk.consecutive_losses_threshold`.

## Found while implementing §7.31 (control-loop integration test)

16. **Agent control-plane key mismatch — latches never reached the real agents**
    (`src/agents/crypto_agent.py`, `src/agents/stocks_agent.py`): both subclasses passed
    `component="crypto_agent"` / `"stocks_agent"` to `BaseTradingAgent`, which keys the
    `agent_control` row by that value. But every consumer — `run_agent(component="crypto")`,
    the control API (`agent_name=component`), and the dashboard's configured agent list
    (`["crypto", "stocks"]`) — uses `"crypto"` / `"stocks"`. Consequences in production:
    pause/close-all latches written by the dashboard/control API were never read by the
    running agents, and agent heartbeats stamped rows nobody watched (dashboard health
    would show `offline` forever). Unit tests missed it because they construct
    `BaseTradingAgent` directly with matching names; no test previously combined the real
    subclasses with the control plane. **Status: fixed in §7.31** — subclass components are
    now `"crypto"` / `"stocks"`, pinned by a new integration test that drives pause and
    close-all through the actual control API against a real `CryptoAgent`.

## Found while implementing §7.35–§7.38 and the §7.28 reconciliation pass (2026-09-22)

17. **Repo-wide `ruff format` drift** (13 files): despite `ruff format .` being a documented
    command, committed files had drifted from the formatter's output (blank-line and wrapping
    differences only — no semantics). Caught by `ruff format --check .` while landing the
    §7.28 reconciliation work; fixed repo-wide in that commit. **Status: fixed.** Consider
    running `ruff format .` (not just `ruff check .`) as part of the per-change routine.

## Found while implementing §7.57–§7.58 (2026-09-25)

18. **Switching an agent between paper and a venue mixes their histories**
    (`core/rehydration.py`): both executors of one agent write to the same agent-scoped
    `orders`/`portfolio_snapshots` rows. §7.58's venue replay skips `paper-…` fills, but
    the reverse is not guarded — a crypto agent that ran keyed on Kraken and is restarted
    on paper rehydrates the venue's last snapshot as *paper* cash/positions and replays
    venue fills into the paper ledger; a Kraken sandbox → live switch likewise replays
    sandbox fills into the live ledger (capped by live balances, so a pre-existing balance
    can surface as a phantom position). **Status: open** — the rows need an execution-venue
    tag (e.g. `orders.venue`) so each executor replays only its own history.

19. **Partial fills of cancelled venue orders never reach the ledger or the DB**
    (`kraken_executor.reconcile_open_orders`, `storage.update_order_status`): only a
    terminal `closed` status feeds the FIFO ledger; an order that partly filled and was then
    cancelled is recorded `cancelled` with its filled amount dropped. The stored `quantity`
    also stays the *requested* size even when the reconciled fill differs, so the §7.58
    replay rebuilds lots from requested, not filled, quantities. **Status: open** — feed
    `filled > 0` of cancelled orders through the ledger and let `update_order_status`
    write the filled quantity.
