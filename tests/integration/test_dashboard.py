"""Dashboard integration tests (§7.15 P3/P4) — seeded DB → rendered pages + control.

Uses httpx's ASGITransport over the *same* loop as storage (aiosqlite never shares a
loop). Covers: monitor pages render from real rows; Pause/Resume/Close-all write the
``agent_control`` latches the agents act on; the safe-config form validates through the
whitelist and rejects credential-shaped keys wholesale; credentials are structurally
absent from every page.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.core.config import Settings
from src.core.storage import Storage
from src.dashboard import create_dashboard_app

# Concrete secrets/paths that must never appear in any dashboard HTML/JSON.
FORBIDDEN = ("api_key", "database_path", ".env", "qwen-test-model", "http://127.0.0.1:1234")


def _settings(tmp_path) -> Settings:
    config = tmp_path / "settings.yaml"
    config.write_text(
        """
llm: {endpoint: "http://127.0.0.1:1234/v1/chat/completions", model: qwen-test-model}
crypto_agent: {enabled: true, interval_minutes: 5, pairs: ["BTC/USDT"], decision_history_limit: 10}
stocks_agent: {enabled: false, interval_minutes: 60, symbols: ["AAPL"], market_hours: "09:00-16:30", decision_history_limit: 10}
risk: {max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}
execution: {paper_fee_pct: 0.0026, paper_slippage_pct: 0.001}
storage: {database_path: "x.db"}
monitoring: {log_level: INFO}
dashboard: {agents: ["crypto", "stocks"], refresh_seconds: 5}
"""
    )
    return Settings(str(config))


async def _seed(storage: Storage) -> None:
    positions = [
        {
            "symbol": "BTC/USDT",
            "quantity": 0.5,
            "avg_entry_price": 50_000.0,
            "current_price": 51_000.0,
            "stop_loss": 49_000.0,
            "take_profit": 53_000.0,
        }
    ]
    await storage.save_portfolio_snapshot(
        cash=9_000.0,
        positions_json=json.dumps(positions),
        total_value=10_100.0,
        unrealized_pnl=500.0,
    )
    await storage.save_llm_decision(
        symbol="BTC/USDT",
        action="buy",
        confidence=0.82,
        reasoning="momentum",
        stop_loss=49_000.0,
        take_profit=53_000.0,
        risk_verdict="approved",
        risk_reason=None,
        realized_pnl=10.0,
    )
    await storage.save_llm_decision(
        symbol="BTC/USDT",
        action="sell",
        confidence=0.61,
        reasoning="take profit",
        stop_loss=None,
        take_profit=None,
        risk_verdict="approved",
        risk_reason=None,
        realized_pnl=-5.0,
    )
    await storage.save_llm_decision(
        symbol="ETH/USDT",
        action="hold",
        confidence=0.2,
        reasoning="weak",
        stop_loss=None,
        take_profit=None,
        risk_verdict="rejected",
        risk_reason="below min confidence",
        is_fallback=False,
    )


@pytest.fixture()
async def env(tmp_path):
    settings = _settings(tmp_path)
    storage = Storage(str(tmp_path / "dash.db"))
    await storage.initialize()
    await _seed(storage)
    app = create_dashboard_app(storage=storage, settings=settings)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://dash")
    yield SimpleNamespace(client=client, storage=storage, settings=settings)
    await client.aclose()
    await storage.close()


def _assert_no_secrets(body: str) -> None:
    low = body.lower()
    for token in FORBIDDEN:
        assert token.lower() not in low, f"forbidden token leaked: {token!r}"


class TestMonitorPages:
    async def test_overview_renders_portfolio_and_health(self, env) -> None:
        resp = await env.client.get("/")
        assert resp.status_code == 200
        body = resp.text
        assert "Overview" in body
        assert "10,100.00" in body  # total_value money-formatted
        assert "crypto" in body and "stocks" in body  # health cards for both agents
        assert "/api/portfolio.json" in body  # uPlot polling wired
        _assert_no_secrets(body)

    async def test_positions_page_shows_open_position(self, env) -> None:
        body = (await env.client.get("/positions")).text
        assert "BTC/USDT" in body
        assert "51,000.00" in body  # current price
        _assert_no_secrets(body)

    async def test_decisions_page_shows_stats_and_rows(self, env) -> None:
        body = (await env.client.get("/decisions")).text
        assert "Win rate" in body
        assert "50.0%" in body  # 1 win / 2 closed
        assert "momentum" in body and "take profit" in body
        _assert_no_secrets(body)

    async def test_portfolio_json_is_safe(self, env) -> None:
        data = (await env.client.get("/api/portfolio.json")).json()
        assert data["total_value"] == [10_100.0]
        assert set(data.keys()) >= {"x", "total_value", "cash", "unrealized_pnl"}
        _assert_no_secrets(json.dumps(data))


class TestControl:
    async def test_pause_resume_round_trip(self, env) -> None:
        frag = (await env.client.post("/control/crypto/pause")).text
        assert "paused" in frag
        row = await env.storage.get_agent_control("crypto")
        assert row is not None and row.state == "paused"

        await env.client.post("/control/crypto/resume")
        row = await env.storage.get_agent_control("crypto")
        assert row is not None and row.state == "running"

    async def test_close_all_latches_for_the_agent(self, env) -> None:
        await env.client.post("/control/crypto/close-all")
        row = await env.storage.get_agent_control("crypto")
        assert row is not None and row.close_all_requested is True

    async def test_unknown_agent_is_404(self, env) -> None:
        assert (await env.client.post("/control/bogus/pause")).status_code == 404
        assert (await env.client.get("/config/bogus")).status_code == 404


class TestConfigForm:
    async def test_get_config_form_is_safe(self, env) -> None:
        body = (await env.client.get("/config/crypto")).text
        assert 'name="risk.min_confidence"' in body
        assert 'name="interval_minutes"' in body
        # Whitelisted numeric default is present; secrets are structurally absent.
        assert "0.6" in body
        _assert_no_secrets(body)

    async def test_valid_overrides_persist(self, env) -> None:
        resp = await env.client.post(
            "/config/crypto",
            data={"interval_minutes": "7", "risk.min_confidence": "0.75"},
        )
        assert resp.status_code == 303
        assert "/config/crypto?saved=1" in resp.headers["location"]

        row = await env.storage.get_agent_control("crypto")
        assert row is not None and "min_confidence" in (row.config_override_json or "")

        merged = (await env.client.get("/config/crypto")).text
        assert "Active overrides" in merged

    async def test_credential_key_is_rejected_wholesale(self, env) -> None:
        resp = await env.client.post("/config/crypto", data={"api_key": "hunter2"})
        assert resp.status_code == 400
        assert "hunter2" not in resp.text  # the value is never echoed

        row = await env.storage.get_agent_control("crypto")
        assert row is None or not row.config_override_json  # nothing saved

    async def test_llm_section_is_rejected(self, env) -> None:
        resp = await env.client.post("/config/crypto", data={"llm.endpoint": "http://evil"})
        assert resp.status_code == 400
        _assert_no_secrets(resp.text)
