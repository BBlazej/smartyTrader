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
- **Demo listings ≠ live listings:** the EEA demo account lists only ~29 EUR spot pairs
  while public data shows ~243 active EUR pairs — never add a symbol to a keyed run's
  traded set without checking it against `load_markets()` at *that* venue
  (`CcxtExecutor.tradable_symbols()`, used by the §7.70 watchlist).
- **`fetch_tickers()` returns a `{symbol: ticker}` dict** (not a list) — iterating it
  yields bare symbol strings (found on the first live screener dry-run;
  `CCXTProvider.fetch_quote_volumes` handles both shapes).
- **`fetch_positions` is a trap for spot:** OKX serves it, but only for
  margin/derivatives — a spot account gets `[]`. `CcxtExecutor` therefore never calls it:
  positions come from its own fill ledger, capped by `fetch_balance()` totals and marked
  each cycle (§7.41/§7.64).
- **Fees:** published spot base tier ≈ 0.08 % maker / 0.10 % taker, but our account
  reports **0.10 % maker / 0.20 % taker** (`fetch_trading_fee`, checked 2026-09-28) — the
  crypto paper profile uses 0.20 % (§7.75). Buy fees are charged in the **base** currency
  (see PLAN §7.65); every fill payload carries its fees.
- **Demo API timeouts:** the demo endpoint intermittently answers `50004 "API endpoint request
  timeout"` (seen on `fetch_order`, 2026-09-28). The order may still have filled; the executor
  leaves it `pending` and reconciliation resolves it later (§7.28/§7.58). Equity dips until then
  (PLAN §7.79).
- **Statuses:** CCXT normalizes exchange statuses. We map `closed → filled`,
  `open/pending → pending`, `canceled/cancelled/expired → cancelled`, `rejected → rejected`
  (see `src/execution/ccxt_executor.py::_STATUS_MAP`).
- **Cancellation needs the symbol:** CCXT's `cancel_order(id, symbol)` requires the
  symbol, so the executor tracks `order_id → symbol` in `_order_symbols`.
- **Rate limits:** set `enableRateLimit: True` on the client. Add exponential
  backoff for `RateLimitExceeded` once we see it in the wild.
- **Balances:** read the quote-currency free balance (`fetch_free_balance`, keyed by
  `crypto_agent.quote_currency` — `EUR`) for the cash figure the risk engine needs.

## Saxo OpenAPI (stocks) — §7.66

Implemented in `src/execution/saxo_client.py` + `saxo_executor.py` (mocked tests only
until a SIM run with a real token; verified against the developer portal and the
`saxo_openapi` wrapper's documented payloads, 2026-09-27).

- **Gateways:** SIM `https://gateway.saxobank.com/sim/openapi` (free simulation account,
  same API as live), LIVE `https://gateway.saxobank.com/openapi`.
- **Auth:** `Authorization: Bearer <token>`. SIM developer tokens from the portal last
  **24 h** — fine for `--once`/manual runs; unattended runs need an OAuth app
  (authorization-code flow + refresh tokens) — PLAN §7.66 follow-up. Env: `SAXO_ACCESS_TOKEN`.
- **Accounts/cash:** `GET /port/v1/accounts/me` → `Data[].AccountKey/ClientKey/Currency`;
  `GET /port/v1/balances?AccountKey=&ClientKey=` → `CashBalance`. The executor trades from
  exactly one account (explicit `account_key`, else the unique active one in
  `account_currency`) and refuses instruments quoted in another currency.
- **Instruments:** `GET /ref/v1/instruments?Keywords=AAPL&AssetTypes=Stock` →
  `Data[].Identifier` (= **Uic**), `Symbol` (`AAPL:xnas`), `CurrencyCode`. Keywords match
  several listings — map data symbols in `saxo_execution.symbol_map`; unmapped symbols are
  accepted only when unambiguous.
- **Orders:** `POST /trade/v2/orders` `{AccountKey, Uic, AssetType: Stock, BuySell,
  Amount (shares), OrderType: Market, OrderDuration: {DurationType: DayOrder},
  ManualOrder: false}` → `{OrderId}`; failures come as `ErrorInfo {ErrorCode, Message}`
  (sometimes with HTTP 200 — treated as errors). Cancel: `DELETE /trade/v2/orders/{id}?AccountKey=`.
- **Fills:** filled orders vanish from `/port/v1/orders/me`; the fill record is the audit log
  `GET /cs/v1/audit/orderactivities?OrderId=&EntryType=Last` → `Status` (`Placed`, `Fill`,
  `FinalFill`, `Cancelled`, …), `FilledAmount`, `AveragePrice`. No streaming — the executor
  polls it a few times after placing, then per cycle (§7.28).
- **Holdings:** `GET /port/v1/netpositions/me?FieldGroups=NetPositionBase,NetPositionView` →
  `NetPositionBase.Amount/Uic/AssetType`, `NetPositionView.CurrentPrice` — net per instrument
  whatever the account's position-netting mode; caps the local FIFO ledger.
- **Market data (open question Q8):** yfinance stays the stocks data source; comparing Saxo's
  own (SIM: delayed) prices is part of the first SIM run.

## XTB Demo (stocks) — DEAD PATH (2026-09-26, §7.66)

> **XTB closed its API access on 2025-03-14** ("XTB no longer offers API access").
> Everything below describes what our client implements; `wss://ws.xapi.pro` is an
> **unofficial third-party relay**, not XTB-sanctioned infrastructure, and can die at
> any time. The code stays in the tree disabled as reference only — PLAN §7.66 replaces
> this path with a Saxo OpenAPI executor (free developer SIM). Do not enable
> `xtb_execution`.

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
