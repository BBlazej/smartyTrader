# API Notes

Quirks and gotchas for the two exchanges, gathered as we integrate.

## Kraken Testnet

- **Sandbox:** use CCXT's `set_sandbox_mode(True)` (our `create_ccxt_provider`
  does this when `testnet: true`). The demo base URL is `https://demo.kraken.com`.
- **Order types:** market, limit, stop-loss, take-profit supported.
- **Statuses:** CCXT normalizes exchange statuses. We map `closed → filled`,
  `open/pending → pending`, `canceled/cancelled → cancelled`, `rejected → rejected`
  (see `src/execution/kraken_executor.py::_STATUS_MAP`).
- **Cancellation needs the symbol:** CCXT's `cancel_order(id, symbol)` requires the
  symbol, so the executor tracks `order_id → symbol` in `_order_symbols`.
- **Rate limits:** set `enableRateLimit: True` on the client. Add exponential
  backoff for `RateLimitExceeded` once we see it in the wild.
- **Balances:** read the quote-currency free balance (`fetch_free_balance("USDT")`)
  for the cash figure the risk engine needs.

## XTB Demo (stocks, Week 5)

- **Access:** xAPI requires registration + approval for API access; the demo
  account is the easier path to start.
- **Auth:** OAuth2 flow — store tokens securely, refresh before expiry.
- **Trading hours:** Warsaw Stock Exchange schedule (09:00–16:30 CET) — the stocks
  agent cycle should gate on this.
- **Instruments:** stocks, CFDs, indices.

## CCXT general

- All exchange calls go through `ccxt.async_support` (imported lazily in the
  factory functions so the rest of the package stays importable without it).
- OHLCV rows are `[ts_ms, open, high, low, close, volume]` —
  `CCXTProvider._to_candle` normalizes them (coercing strings to floats, handling a
  `None` timestamp).
