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
