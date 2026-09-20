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

## XTB Demo (stocks) — as implemented in §7.16

- **Access:** xAPI requires an approved demo account + a one-time **verification
  code** generated in xStation (Settings → xAPI). Set it as `XTB_ACCOUNT_PASSWORD`
  (NOT your login password) alongside `XTB_ACCOUNT_ID`.
- **Auth is NOT OAuth2:** there is no token endpoint. The client connects to
  `wss://ws.xapi.pro/{demo,real}` and sends the classic `login` command; the code
  stays valid ~30 days and is revocable from xStation.
- **Hosts moved:** `ws.xtb.com` / `xapi.xtb.com` were retired 2025-03-14 — older
  libraries and blog posts referencing them are dead (see `nightly_finds.md` #13).
- **Transactions are ordered JSON on one socket** — send a command object, receive
  its response; no request ids. xAPI rate-limits to ~5 req/s, so the client spaces
  commands (`request_interval_seconds`).
- **Instant orders:** `tradeTransaction` with `type=OPEN`, `cmd=0/1`, price = current
  mark; fill confirmed via `tradeTransactionStatus` (requestStatus 3/5 → filled,
  2/4 → rejected). Positions: `getTrades(openedOnly)` marked via one-shot
  `getTickPrices` (bid longs / ask shorts); cash: `getMarginLevel.balance`.
- **Volume is in lots** (≈1 share per lot for XTB equities; check symbol specs).
- **Trading hours:** Warsaw Stock Exchange schedule (09:00–16:30) — already gated
  by the stocks agent's market-hours guard (§7.10).

## CCXT general

- All exchange calls go through `ccxt.async_support` (imported lazily in the
  factory functions so the rest of the package stays importable without it).
- OHLCV rows are `[ts_ms, open, high, low, close, volume]` —
  `CCXTProvider._to_candle` normalizes them (coercing strings to floats, handling a
  `None` timestamp).
