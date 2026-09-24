# API Notes

Quirks and gotchas for the two exchanges, gathered as we integrate.

## Kraken (spot)

- **There is no spot sandbox** (verified against ccxt 4.5: `kraken().urls['test']`
  is `None`, and `set_sandbox_mode(True)` raises an opaque `TypeError`; only
  `krakenfutures` has `demo-futures.kraken.com`). `create_ccxt_provider(testnet=True)`
  now raises an actionable `ValueError` for exchanges without one, and the crypto
  runner keeps a keyed Kraken spot setup on **paper** unless `testnet: false`,
  `live_trading: true` and `LIVE_TRADING_ACK` are all set — it is real money (§7.41).
- **No `fetch_positions` on spot:** positions come from the executor's own fill ledger,
  capped by `fetch_balance()` totals and marked each cycle (§7.41).
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
  mark; fill confirmed via `tradeTransactionStatus` (documented REQUEST_STATUS: 0 error,
  1 pending, 3 accepted → filled, 4 rejected; unknown codes stay pending).
- **Closing is a separate transaction (§7.40):** `cmd=SELL, type=OPEN` *opens a short*.
  Close with `type=CLOSE` (2), the trade's opening `cmd` and its `order` number from
  `getTrades` (partial volume allowed) — `XApiClient.close_trade`. Positions: `getTrades(openedOnly)` marked via one-shot
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
