"""Dashboard integration tests (§7.15 P3/P4) — seeded DB → rendered pages + control.

Uses httpx's ASGITransport over the *same* loop as storage (aiosqlite never shares a
loop). Covers: monitor pages render from real rows; Pause/Resume/Close-all write the
``agent_control`` latches the agents act on; the safe-config form validates through the
whitelist and rejects credential-shaped keys wholesale; credentials are structurally
absent from every page.
"""

from __future__ import annotations

import json
import sys
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


class TestLogViewer:
    async def test_missing_log_shows_hint(self, env) -> None:
        body = (await env.client.get("/logs/crypto")).text
        assert "No log file yet" in body

    async def test_page_tails_written_log(self, env, tmp_path) -> None:
        (tmp_path / "agent_crypto.out.log").write_text(
            "[2026-09-21 20:34:41][info] cycle start symbols=['BTC/USDT']\n"
            "[2026-09-21 20:34:42][info] cycle end executed=0\n"
        )
        body = (await env.client.get("/logs/crypto")).text
        assert "cycle start" in body and "cycle end" in body

    async def test_partial_refreshes_tail(self, env, tmp_path) -> None:
        (tmp_path / "agent_crypto.out.log").write_text("first line\n")
        assert "first line" in (await env.client.get("/logs/crypto/partial")).text
        (tmp_path / "agent_crypto.out.log").write_text("second line\n")
        body = (await env.client.get("/logs/crypto/partial")).text
        assert "second line" in body and "first line" not in body

    async def test_unknown_agent_404(self, env) -> None:
        assert (await env.client.get("/logs/nonexistent")).status_code == 404


class TestLaunchDisabled:
    """Without dashboard.allow_launch (the default) there is no supervision at all."""

    async def test_launch_endpoints_forbidden(self, env) -> None:
        resp = await env.client.post("/launch/crypto/start")
        assert resp.status_code == 403

    async def test_no_start_buttons_rendered(self, env) -> None:
        body = (await env.client.get("/partials/health")).text
        assert "/launch/" not in body


class TestLaunch:
    """Supervisor wired with an injected launcher (sleep commands, never agents)."""

    @pytest.fixture()
    async def lenv(self, tmp_path):
        from src.dashboard.launch import AgentLauncher

        settings = _settings(tmp_path)
        storage = Storage(str(tmp_path / "dash.db"))
        await storage.initialize()
        launcher = AgentLauncher(
            tmp_path,
            command_builder=lambda agent: [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ],
        )
        app = create_dashboard_app(storage=storage, settings=settings, launcher=launcher)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://dash")
        yield SimpleNamespace(client=client, storage=storage, launcher=launcher)
        for agent in ("crypto", "stocks"):
            await launcher.stop(agent)  # never leave a sleep process behind
        await client.aclose()
        await storage.close()

    async def test_start_then_stop_round_trip(self, lenv) -> None:
        resp = await lenv.client.post("/launch/crypto/start")
        assert resp.status_code == 200
        assert "/launch/crypto/stop" in resp.text  # card now offers Stop
        pid = lenv.launcher.managed_pid("crypto")
        assert pid is not None

        resp = await lenv.client.post("/launch/crypto/stop")
        assert resp.status_code == 200
        assert "/launch/crypto/start" in resp.text  # back to offering Start
        assert lenv.launcher.managed_pid("crypto") is None

    async def test_start_refused_when_fresh_heartbeat(self, lenv) -> None:
        # An agent already trading (started elsewhere) must not get a twin launched.
        await lenv.storage.record_cycle_health("crypto")
        resp = await lenv.client.post("/launch/crypto/start")
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]

    async def test_start_refused_for_disabled_agent(self, lenv) -> None:
        # stocks_agent is enabled: false — its runner would exit at the enabled-gate.
        resp = await lenv.client.post("/launch/stocks/start")
        assert resp.status_code == 409

    async def test_stop_refused_for_foreign_process(self, lenv) -> None:
        resp = await lenv.client.post("/launch/crypto/stop")
        assert resp.status_code == 409


class TestMonitorPages:
    async def test_overview_renders_portfolio_and_health(self, env) -> None:
        resp = await env.client.get("/")
        assert resp.status_code == 200
        body = resp.text
        assert "Overview" in body
        assert "10,100.00" in body  # total_value money-formatted
        assert "crypto" in body and "stocks" in body  # health cards for both agents
        assert "/api/portfolio.json" in body  # uPlot polling wired
        # No heartbeat rows seeded → an enabled agent must NOT show as running.
        assert "badge b-offline" in body
        _assert_no_secrets(body)

    async def test_fresh_heartbeat_shows_running(self, env) -> None:
        await env.storage.record_cycle_health("crypto")
        body = (await env.client.get("/partials/health")).text
        assert "badge b-running" in body
        assert ">running</span>" in body

    async def test_disabled_agent_shows_disabled(self, env) -> None:
        # stocks_agent has enabled: false in the test settings.
        body = (await env.client.get("/partials/health")).text
        assert "disabled" in body

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
