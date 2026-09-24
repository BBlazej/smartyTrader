"""Control API contract tests (§7.15 P2) — ASGI-in-process client over real SQLite.

All calls go through httpx's ASGITransport on the *same* pytest-asyncio loop as the
storage, so aiosqlite connections are never shared across event loops.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.core.config import Settings
from src.core.control_api import create_control_app
from src.core.storage import Storage


def _settings(tmp_path) -> Settings:
    config = tmp_path / "settings.yaml"
    config.write_text(
        """
llm: {endpoint: "http://localhost:1234/v1/chat/completions", model: m}
crypto_agent: {enabled: true, interval_minutes: 5, pairs: ["BTC/USDT"], decision_history_limit: 10}
stocks_agent: {enabled: false, interval_minutes: 60, symbols: ["AAPL"], market_hours: "08:00-22:00", decision_history_limit: 10}
risk: {max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}
storage: {database_path: "x.db"}
monitoring: {log_level: INFO}
"""
    )
    return Settings(str(config))


@pytest.fixture()
async def env(tmp_path):
    """Fresh storage + control app (crypto agent) with stubbed live positions."""
    settings = _settings(tmp_path)
    # Bound like the runner's storage (§7.39): the control API serves one agent's rows.
    storage = Storage(str(tmp_path / "api.db"), agent="crypto")
    await storage.initialize()
    position = SimpleNamespace(
        model_dump=lambda mode="json": {"symbol": "BTC/USDT", "quantity": 1.0}
    )
    app = create_control_app(
        storage=storage,
        agent_name="crypto",
        settings=settings,
        get_positions=AsyncMock(return_value=[position]),
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://control")
    yield SimpleNamespace(client=client, storage=storage, settings=settings)
    await client.aclose()
    await storage.close()


class TestControlActions:
    async def test_pause_resume_round_trip(self, env) -> None:
        resp = await env.client.post("/api/agents/crypto/pause")
        assert resp.json() == {"agent": "crypto", "state": "paused"}

        row = await env.storage.get_agent_control("crypto")
        assert row is not None and row.state == "paused"

        status = (await env.client.get("/api/agents/crypto")).json()
        assert status["state"] == "paused"

        await env.client.post("/api/agents/crypto/resume")
        assert (await env.client.get("/api/agents/crypto")).json()["state"] == "running"

    async def test_default_state_without_any_row(self, env) -> None:
        status = (await env.client.get("/api/agents")).json()[0]
        assert status["state"] == "running"
        assert status["close_all_requested"] is False
        assert status["last_cycle_at"] is None
        assert status["positions"] == [{"symbol": "BTC/USDT", "quantity": 1.0}]

    async def test_close_all_sets_the_latch_for_the_agent(self, env) -> None:
        resp = await env.client.post("/api/agents/crypto/close-all")
        assert resp.status_code == 200
        assert resp.json()["close_all_requested"] is True

        row = await env.storage.get_agent_control("crypto")
        assert row is not None and row.close_all_requested is True  # agent consumes next cycle

    async def test_other_agents_are_404(self, env) -> None:
        assert (await env.client.post("/api/agents/stocks/pause")).status_code == 404
        assert (await env.client.get("/api/agents/stocks/decisions")).status_code == 404


class TestReadEndpoints:
    async def test_decisions_endpoint_limits_newest_first(self, env) -> None:
        first = await env.storage.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning="r1",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
        second = await env.storage.save_llm_decision(
            symbol="BTC/USDT",
            action="hold",
            confidence=0.0,
            reasoning="fallback",
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
            is_fallback=True,
        )
        # Separate the timestamps deterministically (same-microsecond saves tie).
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import text

        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        async with await env.storage._session() as session:
            # String bind in SQLAlchemy's SQLite DATETIME format — raw datetime
            # params on text() SQL hit sqlite3's deprecated adapter (§7.29).
            await session.execute(
                text("UPDATE llm_decisions SET timestamp = :old WHERE id = :rid"),
                {"old": old.strftime("%Y-%m-%d %H:%M:%S.%f"), "rid": first},
            )
            await session.commit()

        rows = (await env.client.get("/api/agents/crypto/decisions?limit=1")).json()

        assert len(rows) == 1
        assert {rows[0]["id"]} == {second}
        assert rows[0]["is_fallback"] is True
        assert first != second

    async def test_portfolio_endpoint(self, env) -> None:
        await env.storage.save_portfolio_snapshot(
            cash=9_000.0, positions_json="[]", total_value=10_100.0, unrealized_pnl=100.0
        )

        payload = (await env.client.get("/api/agents/crypto/portfolio")).json()

        assert payload["latest"]["total_value"] == pytest.approx(10_100.0)
        assert len(payload["history"]) == 1


class TestConfigEndpoints:
    async def test_get_config_exposes_only_safe_fields(self, env) -> None:
        body = json.dumps((await env.client.get("/api/config")).json())

        # Credentials / LLM plumbing are structurally absent from the payload.
        for forbidden in ("llm", "endpoint", "api_key", "secret", "database_path"):
            assert forbidden not in body.lower()

        payload = (await env.client.get("/api/config")).json()
        assert payload["config"]["risk"]["min_confidence"] == pytest.approx(0.6)
        assert payload["agent_config"]["interval_minutes"] == 5

    async def test_put_valid_overrides_persist_and_merge(self, env) -> None:
        resp = await env.client.put(
            "/api/config",
            json={"decision_history_limit": 3, "risk": {"min_confidence": 0.75}},
        )
        assert resp.status_code == 200

        row = await env.storage.get_agent_control("crypto")
        assert row is not None and "min_confidence" in (row.config_override_json or "")

        merged = (await env.client.get("/api/config")).json()
        assert merged["config"]["overrides"]["risk"]["min_confidence"] == 0.75
        assert merged["agent_config"]["decision_history_limit"] == 3

    async def test_put_credential_key_is_rejected(self, env) -> None:
        resp = await env.client.put("/api/config", json={"api_key": "hunter2"})
        assert resp.status_code == 400
        assert "hunter2" not in resp.text

        merged = (await env.client.get("/api/config")).json()
        assert merged["config"]["overrides"] == {}  # nothing was saved

    async def test_put_llm_section_is_rejected(self, env) -> None:
        resp = await env.client.put("/api/config", json={"llm": {"endpoint": "http://evil"}})
        assert resp.status_code == 400
