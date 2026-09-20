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
   is still outstanding before "Kraken testnet mode" is offered. **Status: open.**

2. **`yfinance` is not installed in the dev venv**, so the stocks provider's real
   network path (and the §7.11 data-depth fix) can only be exercised through the
   injected-source seam; no live yfinance validation from this sandbox. **Status: open.**

## Code nits noticed en route (candidates for §7.19)

3. **Daily-loss boundary is float-fragile** (`risk_engine.py`, seen while adding
   hypothesis tests): the rule fires at `pnl_pct <= -limit` exactly; whether a
   drop of *exactly* `-2.000…%` trips it depends on float rounding of the specific
   values. Not wrong, but tests asserting the exact boundary are brittle (one
   property test had to widen its range). Consider `>=` with a small epsilon or
   documenting "strictly beyond". **Status: open.**

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
   quantity) into the tracker at startup. **Status: open.**

8. **XTB tracking only works for priced orders** (`xtb_executor.py`): the xAPI seam's
   `create_order` payload is mapped without a fill price, so a *market* order that
   fills has no basis to record and its later close reports no outcome. The pipeline
   always sends marketable limits, so it does not bite today; real xAPI work (§7.16)
   should read fills from the venue's order/position stream instead. **Status: open.**

9. **`closed_entries` assumes a single-sided (long-only) book**: the tracker consumes
   lots on sells only, matching this system's spot-only usage. If shorts are ever
   supported (see #4), `PositionTracker.on_sell` must also handle opening shorts and
   buys closing them. **Status: open (by design for now).**

## Found while implementing §7.14 (decision-replay backtesting)

10. **Risk-engine trackers are wall-clock-bound during replay** (`risk_engine.py`
    via `backtester.py`): `DailyLossTracker` and the losing-streak cooldown derive
    "today" from `datetime.now(UTC)`, so a replay of months of decisions behaves as
    one continuous day — the daily-loss cap becomes a whole-window cap, and
    cooldowns are relative to replay execution rather than each decision's own
    date. Live behavior is correct; only replay fidelity is affected. A fix means
    injecting a clock into `RiskEngine` (and updating its many tests); for now the
    limitation is documented in the backtester module docstring and CLI output.
    **Status: open (documented).**

11. **Sharpe annualization is coarse for non-24/7 series** (`backtester.py`):
    equity-curve returns are scaled by √(timeframe's nominal periods/year), which
    ignores weekends/holiday gaps in stock candles — stock Sharpes are overstated
    relative to crypto. Acceptable for v1 comparison; revisit with calendar-aware
    period counts if the stocks backtest becomes important. **Status: open
    (documented).**

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
    streaming channel. **Status: implemented accordingly (§7.16).**
