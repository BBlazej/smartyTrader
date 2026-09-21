# External Architecture & Code Review 3 — Autonomous Trading Agent

**Date:** 2026-09-21  
**Scope:** Full codebase audit covering `src/` (core, agents, analysis, data, execution, monitoring, dashboard), `scripts/`, `tests/`, `config/`, `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `AGENTS.md`, `ARCHITECTURE.md`, `PLAN.md`, `HISTORY.md`, and `nightly_finds.md`.

---

## 1. Verification & Automated Test Status

- **`pytest` Test Suite:** **495 passed** in 3.90 seconds. Overall line coverage across `src/` is **93%**.
- **`ruff check .`:** Clean (**0 errors** across all modules).
- **Codebase Seams:** All external API dependencies (CCXT, xAPI WebSocket, yfinance, LM Studio) are properly isolated behind Protocol interfaces and fully mockable in unit and integration tests without network access.

---

## 2. Executive Summary & Evolution

Since the prior reviews (`review.MD` on 2026-09-15 and `review2.md` on 2026-09-17), the project has undergone significant, disciplined expansion:
1. **Full Operational Dashboard & Control Plane (§7.15):** Standalone FastAPI + HTMX/Jinja2 web interface (`src/dashboard/`) reading SQLite WAL mode and managing control latches (`agent_control`), with strict safe-config whitelist validation (`SafeConfigOverrides`, `extra="forbid"`) forbidding credential injection.
2. **Real XTB Demo Execution via xAPI (§7.16):** Asynchronous WebSocket client (`execution/xtb_client.py`) using xStation verification codes, instant order placement, tick-marked position tracking, and fill polling.
3. **Decision-Replay Backtesting (§7.14):** Deterministic re-simulation of stored LLM decisions over fresh historical candles (`core/backtester.py`) using identical risk gate and paper fee/slippage rules, zero LLM calls.
4. **Analysis Layer Extraction (§7.17):** Technical indicator math (`analysis/indicators.py`) and prompt formatting (`analysis/prompt_builder.py`) decoupled from the pipeline orchestrator.
5. **Process Supervision & Lifecycle (§7.24 & §7.13):** Opt-in process launcher (`dashboard/launch.py`), unified runner lifecycle (`core/runner.py`), and heartbeat-derived agent liveness (`dashboard/views.py`).

The architecture demonstrates outstanding software craft, strict safety-first design (risk gate execution bounds, fail-soft control reads, loopback-only defaults), and high test performance. This review highlights remaining edge cases in state rehydration, test warnings, and minor protocol documentation drift.

---

## 3. High / Critical Severity Findings

### H1. Hardcoded Consecutive-Loss Threshold in State Rehydration
- **Location:** [`src/core/rehydration.py:98`](file:///home/blazej/AI_projects/trading_agent/src/core/rehydration.py#L98)
- **Problem:** When rehydrating the loss streak and cooldown state from SQLite at startup, the function uses a hardcoded threshold of `3`:
  ```python
  if streak >= 3 and newest_loss_ts is not None:
  ```
- **Impact:** If a user configures `risk.consecutive_losses_threshold` to a value other than `3` (e.g. `2` or `5`) in `config/settings.yaml`, process restarts will evaluate the trailing loss streak against `3` instead of the configured risk setting.
- **Fix:** Pass `risk_engine.settings.consecutive_losses_threshold` to `rehydrate_risk_engine` and compare `streak >= threshold`.

### H2. Un-rehydrated FIFO Position Tracker Across Restarts
- **Location:** [`src/execution/paper_executor.py:250`](file:///home/blazej/AI_projects/trading_agent/src/execution/paper_executor.py#L250), [`src/execution/kraken_executor.py`](file:///home/blazej/AI_projects/trading_agent/src/execution/kraken_executor.py), [`src/execution/xtb_executor.py`](file:///home/blazej/AI_projects/trading_agent/src/execution/xtb_executor.py) *(nightly_finds #7)*
- **Problem:** `PaperExecutor.load_portfolio_state` rehydrates cash and `Position` objects from the latest `portfolio_snapshots` row, but it does **not** rebuild the internal `PositionTracker` (`self._tracker`) FIFO lot ledger.
- **Impact:** Positions opened prior to an agent restart lose their lot purchase history and entry decision IDs. When a position opened before a restart is later closed, `_tracker.on_sell()` finds zero tracked entry lots (`quantity == 0`). Consequently:
  1. Paper execution reports `realized_pnl = 0.0` or missing outcome PnL.
  2. `closed_entries` is empty (`[]`), so the closing sell cannot attribute realized PnL back to the entry decision's row in SQLite via `Storage.add_realized_pnl`.
- **Fix:** In `rehydration.py`, query historical `orders` rows (buy fills containing `decision_id`, `price`, and `quantity`) and replay them into `PositionTracker` at startup.

---

## 4. Medium Severity Findings

### M1. Wall-Clock Time Coupling in Risk Engine during Backtest Replay
- **Location:** [`src/core/risk_engine.py`](file:///home/blazej/AI_projects/trading_agent/src/core/risk_engine.py#L35), [`src/core/backtester.py`](file:///home/blazej/AI_projects/trading_agent/src/core/backtester.py#L14) *(nightly_finds #10)*
- **Problem:** `DailyLossTracker` and `ConsecutiveLossTracker` rely directly on `datetime.now(UTC)` to determine current date boundaries and cooldown expiry. When running `DecisionReplayBacktester`, wall-clock time does not advance as historical decision timestamps progress across months of candles.
- **Impact:** During backtest replay, the daily-loss limit functions as a continuous whole-window loss cap rather than resetting per simulated calendar day, and losing-streak cooldowns expire relative to the backtest execution time rather than simulated candle timestamps.
- **Fix:** Inject a clock interface or timestamp provider into `RiskEngine` (defaulting to `datetime.now(UTC)` for live execution) and pass the decision candle timestamp during replay.

### M2. Test Suite Deprecation & Unawaited Async Coroutine Warnings
- **Location:** [`tests/unit/test_control_plane.py`](file:///home/blazej/AI_projects/trading_agent/tests/unit/test_control_plane.py#L237), [`tests/unit/test_storage.py`](file:///home/blazej/AI_projects/trading_agent/tests/unit/test_storage.py)
- **Problem:** Executing `pytest` outputs 19 warnings:
  1. `aiosqlite` / `sqlite3` deprecation warnings on default datetime adapters in Python 3.12+.
  2. `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` when `risk_engine.update_daily_value` (a synchronous method) is mocked asynchronously in `test_control_plane.py`.
- **Impact:** Clutters test output and signals potential incompatibilities with Python 3.14+.
- **Fix:** Replace `AsyncMock` with standard `MagicMock` for synchronous `RiskEngine` methods in test suites, and explicitly handle ISO string conversions for `aiosqlite` datetime queries.

### M3. Stale Protocol/OAuth2 Documentation in `XTBExecutor`
- **Location:** [`src/execution/xtb_executor.py:10-14`](file:///home/blazej/AI_projects/trading_agent/src/execution/xtb_executor.py#L10-L14)
- **Problem:** The module docstring still states that XTB requires an *"approved demo account plus an OAuth2 flow"* and that paper execution is the only option until it lands.
- **Impact:** Documentation drift. §7.16 delivered full WebSocket xAPI demo execution (`execution/xtb_client.py`), using classic `login` credentials over `wss://ws.xapi.pro` (no OAuth2 exists in the xAPI spec).
- **Fix:** Update the docstring in `xtb_executor.py` to reflect the completed §7.16 implementation.

---

## 5. Low Severity Findings & Operational Nits

### L1. Floating-Point Boundary Precision in Daily Loss Check
- **Location:** [`src/core/risk_engine.py:270`](file:///home/blazej/AI_projects/trading_agent/src/core/risk_engine.py#L270) *(nightly_finds #3)*
- **Problem:** `_check_daily_loss` evaluates `pnl_pct <= -self.settings.daily_loss_limit_pct`. Floating-point arithmetic near boundary numbers (e.g. `-0.020000000000000004` vs `-0.019999999999999998`) can lead to subtle boundary test brittleness.
- **Recommendation:** Use a small epsilon tolerance or document strict inequality requirements.

### L2. `Position` Model Short-Side Representation Gap
- **Location:** [`src/core/models.py::Position`](file:///home/blazej/AI_projects/trading_agent/src/core/models.py) *(nightly_finds #4)*
- **Problem:** `Position` has a positive `quantity` field without a `side` (long/short) enum. `KrakenExecutor.get_positions` maps short CCXT positions to positive `quantity`.
- **Recommendation:** For spot-only trading this is acceptable, but any future margin or derivatives expansion must add an explicit `side` field to `Position`.

### L3. Non-Calendar Sharpe Annualization in Backtester
- **Location:** [`src/core/backtester.py:43-53`](file:///home/blazej/AI_projects/trading_agent/src/core/backtester.py#L43-L53) *(nightly_finds #11)*
- **Problem:** Equity curve returns are scaled using nominal periods per year (`365.0` for daily candles). Stocks trade ~252 days/year due to weekends and holidays, slightly overstating stock Sharpe ratios relative to 24/7 crypto series.
- **Recommendation:** Implement calendar-aware period counting when comparing stock strategies against crypto strategies.

### L4. $O(N^2)$ MACD Recalculation Loop
- **Location:** [`src/analysis/indicators.py:118-125`](file:///home/blazej/AI_projects/trading_agent/src/analysis/indicators.py#L118-L125)
- **Problem:** `_compute_macd` recalculates EMAs over expanding slices from index 0 for each candle index $i \in [26, N]$. While fast for $N=100$, it performs redundant calculations.
- **Recommendation:** Compute EMA series in a single linear pass ($O(N)$).

---

## 6. What's Genuinely Good

- **Deterministic Safety Gating:** Execution remains strictly impossible without passing `RiskEngine.evaluate`. Sizing limits and position checks execute prior to order submission.
- **Zero-Trust Dashboard Security:** The dashboard writes latches to SQLite without holding API credentials or direct HTTP handles to agent processes. Form inputs are strictly validated against `SafeConfigOverrides` (`extra="forbid"`).
- **Restart Rehydration & Resiliency:** Paper state, peak equity (drawdown high-water mark), and daily-loss baselines rehydrate seamlessly at startup, preventing state wiping across process restarts.
- **High Test Quality:** 495 tests executing in under 4 seconds with 93% line coverage guarantees rapid iteration without regression risk.

---

## 7. Recommended Action Plan

1. **Fix Rehydration Threshold Hardcoding (H1):** Update `rehydrate_risk_engine` in `src/core/rehydration.py` to use `risk_engine.settings.consecutive_losses_threshold`.
2. **Rehydrate `PositionTracker` FIFO Ledgers (H2):** Replay historical filled `orders` into `PositionTracker` during startup rehydration so pre-restart positions retain cost basis and entry decision IDs.
3. **Clean Up Test Warnings (M2):** Replace `AsyncMock` with `MagicMock` for `update_daily_value` in `test_control_plane.py`.
4. **Update Stale Docstrings (M3):** Align `src/execution/xtb_executor.py` documentation with the delivered §7.16 xAPI WebSocket client.
