# API Notes

Quirks and gotchas for the venues, gathered as we integrate.

## OKX Europe (crypto spot) — §7.64

- **Which OKX:** EEA residents are served by OKX Europe (Malta, MiCA-licensed). In ccxt
  that is **`myokx`** (host `eea.okx.com`), not `okx` (global). EU accounts trade
  **EUR/USDC-quoted** pairs only — USDT is not tradable for EEA accounts (MiCA).
  Verified 2026-09-26: `BTC/EUR` (min 0.0001 BTC) and `ETH/EUR` (min 0.001 ETH) listed,
  BTC/EUR spread ≈ 0.0001 %, 272 active EUR spot pairs.
- **Credentials:** API key + secret + **passphrase** (ccxt `password`) — our env vars
  `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` / `EXCHANGE_API_PASSPHRASE`.
- **Demo trading:** same host; ccxt `set_sandbox_mode(True)` adds the
  `x-simulated-trading: 1` header. Demo needs its **own** API key (OKX → Trade → Demo
  Trading → Demo Trading API). `testnet: true` selects this.
- **`fetch_positions` is a trap for spot:** OKX serves it, but only for
  margin/derivatives — a spot account gets `[]`. `CcxtExecutor` therefore never calls it:
  positions come from its own fill ledger, capped by `fetch_balance()` totals and marked
  each cycle (§7.41/§7.64).
- **Fees:** spot base tier ≈ 0.08 % maker / 0.10 % taker (lower EU spot-only fees from
  2026-09-25 — check the account); buy fees are charged in the **base** currency (see
  PLAN §7.65).
- **Statuses:** CCXT normalizes exchange statuses. We map `closed → filled`,
  `open/pending → pending`, `canceled/cancelled/expired → cancelled`, `rejected → rejected`
  (see `src/execution/ccxt_executor.py::_STATUS_MAP`).
- **Cancellation needs the symbol:** CCXT's `cancel_order(id, symbol)` requires the
  symbol, so the executor tracks `order_id → symbol` in `_order_symbols`.
- **Rate limits:** set `enableRateLimit: True` on the client. Add exponential
  backoff for `RateLimitExceeded` once we see it in the wild.
- **Balances:** read the quote-currency free balance (`fetch_free_balance`, keyed by
  `crypto_agent.quote_currency` — `EUR`) for the cash figure the risk engine needs.

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
