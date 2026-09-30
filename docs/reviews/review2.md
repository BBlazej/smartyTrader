# Architecture Review 2 — Undocumented TODO Tasks

**Date:** 2026-09-17  
**Source:** Full Architectural Review (`architecture_review.md`)  
**Scope:** Actionable TODO items and architectural improvements discovered during the 2026-09-17 comprehensive review that are **not yet documented** in `PLAN.md`, `AGENTS.md`, or prior review files (`review.MD`, `nightly_finds.md`).

---

## 1. Safety & State Hydration

### 1.1 Rehydrate Venue FIFO Ledgers on Startup
- **Problem:** `PositionTracker` (used by `PaperExecutor`, `KrakenExecutor`, `XTBExecutor`) tracks entry lots in memory. Across an agent restart, positions opened prior to restart lose their lot history, so closing fills report no outcome PnL (`closed_entries: []`).
- **TODO:** At startup in `core/rehydration.py`, query historical `orders` rows from SQLite and replay them into `PositionTracker` so entry decisions and lot purchase prices are preserved across process restarts.
- **Priority:** High

### 1.2 Clock Injection for Risk Engine (Backtest Replay Fidelity)
- **Problem:** `RiskEngine` relies on `datetime.now(UTC)` for daily-loss and consecutive-loss cooldown trackers. During backtest replay (`scripts/backtest.py`), wall-clock time does not advance, making the backtester treat the entire replay period as a single continuous day.
- **TODO:** Inject a `Clock` protocol / callable into `RiskEngine` (defaulting to `datetime.now(UTC)` for live trading) so the backtester can pass historical block/candle timestamps to evaluate daily loss caps per simulated day.
- **Priority:** High

---

## 2. LLM Reliability & Resilience

### 2.1 Exponential / Linear Backoff for LLM Retries
- **Problem:** `llm_client.py` performs 3 retries on failure with no sleep / backoff (`await asyncio.sleep(0)` equivalent). When a local LLM (LM Studio / GPU) is struggling or overloaded, immediate retries intensify server collapse.
- **TODO:** Implement exponential backoff (e.g. 1s, 2s, 4s) or configurable delay between retries in `LLMClient.send_prompt`.
- **Priority:** Medium

### 2.2 Expose Temperature, Seed, and Max Tokens in Config
- **Problem:** `temperature=0.2` and `max_tokens=1024` are currently hardcoded in `llm_client.py`. There is no seed parameter support or config binding, preventing deterministic LLM evaluation or prompt length tuning.
- **TODO:** 
  1. Add `temperature`, `max_tokens`, and optional `seed` fields to `LLMSettings` in `core/config.py` and `config/settings.yaml`.
  2. Pass them to the LM Studio `/v1/chat/completions` API payload.
- **Priority:** Medium

### 2.3 Robust Endpoint & URL Normalization in `LLMClient`
- **Problem:** `llm_client.py` derives its base URL by parsing `endpoint.rsplit("/v1", 1)[0]` and re-appending `/v1/chat/completions`. This string manipulation is fragile if custom proxy URLs or non-standard paths are configured.
- **TODO:** Use `urllib.parse` / `httpx.URL` for clean URL manipulation in `llm_client.py`.
- **Priority:** Low

### 2.4 Prompt & Response Payload Sanitization / Size Guards
- **Problem:** No explicit upper bound check on prompt length or raw LLM response size before attempting JSON parsing.
- **TODO:** Truncate or reject oversized raw responses before parsing, and raise a structured `LLMResponseError` on malformed binary or non-JSON payloads.
- **Priority:** Low

---

## 3. Testing & Integration Gaps

### 3.1 End-to-End Control API <-> Agent Loop Integration Test
- **Problem:** `tests/unit/test_control_api.py` tests FastAPI endpoints in isolation, but no integration test verifies that `POST /api/agents/crypto/pause` or `POST /api/agents/crypto/close-all` actually pauses cycles or executes `close_all_positions` in a running `BaseTradingAgent`.
- **TODO:** Write an integration test in `tests/integration/` where a running `BaseTradingAgent` executes a cycle, receives a control command via HTTP/DB, and responds correctly (pausing or executing emergency close).
- **Priority:** Medium

### 3.2 APScheduler Concurrency & Misfire Tests
- **Problem:** APScheduler executes agent cycles asynchronously. There are no tests covering cycle overlap (when a cycle takes longer than `interval_minutes`), misfires, or concurrent symbol fetching under error conditions.
- **TODO:** Add unit/integration tests in `tests/unit/test_scheduler.py` for job misfire policies, overlapping cycle prevention, and async execution safety.
- **Priority:** Medium

---

## 4. Storage & Operations

### 4.1 SQLite Automatic Backup (`.backup`) in Maintenance Job
- **Problem:** SQLite WAL mode provides concurrent read access, but the single `.db` file has no automated point-in-time backup. A crash or disk corruption could lose trading history.
- **TODO:** Add a `.backup()` call using `aiosqlite` connection in `core/retention.py` to create a timestamped database backup during the daily retention pruning job.
- **Priority:** Low

### 4.2 Storage Repository Decomposition (`storage.py`)
- **Problem:** `storage.py` is approaching ~450 LOC and handles schema initialization, raw SQL migrations, snapshots, decisions, orders, portfolio state, and `agent_control` tables all in one class.
- **TODO:** Split `storage.py` into focused sub-modules under `core/storage/` (e.g. `migrations.py`, `snapshots.py`, `decisions.py`, `control.py`) while keeping `Storage` as an orchestrating facade.
- **Priority:** Low
