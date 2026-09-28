"""Dashboard over multiple per-mode books (§7.78).

Covers what the single-file suite cannot: ``open_books`` discovery (and its legacy
shared-file fallback), per-book health cards + mode badges, a latch write landing in
*that file only*, ambiguous ``?agent=`` resolution refusing to pick between paper and
demo of one agent, decisions merged across books, and per-book portfolio JSON.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.core import db_layout
from src.core.config import Settings
from src.core.storage import Storage
from src.dashboard import create_dashboard_app
from src.dashboard.books import find_book, open_books
from src.dashboard.launch import AgentLauncher


def _settings(tmp_path, *, data_dir=None, database_path=None) -> Settings:
    config = tmp_path / "settings.yaml"
    storage_line = f'  database_path: "{database_path}"\n' if database_path else ""
    if data_dir:
        storage_line += f'  data_dir: "{data_dir}"\n'
    config.write_text(
        f"""
llm: {{endpoint: "http://127.0.0.1:1234/v1/chat/completions", model: qwen-test-model}}
crypto_agent: {{enabled: true, interval_minutes: 5, pairs: ["BTC/USDT"]}}
stocks_agent: {{enabled: false, interval_minutes: 60, symbols: ["AAPL"]}}
risk: {{max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}}
execution: {{paper_fee_pct: 0.0026, paper_slippage_pct: 0.001}}
storage: {{
{storage_line}}}
monitoring: {{log_level: INFO}}
dashboard: {{agents: ["crypto", "stocks"], refresh_seconds: 5}}
"""
    )
    return Settings(str(config))


async def _seed_book(path: str, *, reasoning: str, total_value: float) -> None:
    """Seed one book file the way that runner writes it (agent-bound handle)."""
    bound = Storage(path, agent="crypto")
    await bound.initialize()
    try:
        await bound.save_portfolio_snapshot(
            cash=total_value,
            positions_json=json.dumps([]),
            total_value=total_value,
            unrealized_pnl=0.0,
        )
        await bound.save_llm_decision(
            symbol="BTC/USDT",
            action="buy",
            confidence=0.8,
            reasoning=reasoning,
            stop_loss=None,
            take_profit=None,
            risk_verdict="approved",
            risk_reason=None,
        )
    finally:
        await bound.close()


def _client(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        headers={"X-CSRF-Token": app.state.csrf_token},
    )


@pytest.fixture()
async def books_env(tmp_path):
    """Two crypto books (paper + demo) in ``data_dir``, as after ``split_database``."""
    data_dir = tmp_path / "books"
    data_dir.mkdir()
    paths = {
        "paper": db_layout.db_path(data_dir, "paper", "crypto"),
        "demo": db_layout.db_path(data_dir, "demo", "crypto"),
    }
    await _seed_book(str(paths["paper"]), reasoning="paper-only-momentum", total_value=5_050.0)
    await _seed_book(str(paths["demo"]), reasoning="demo-only-momentum", total_value=9_900.0)

    settings = _settings(tmp_path, data_dir=str(data_dir))
    books = await open_books(settings)
    # A real launcher (nothing is ever started in these tests) so /launch/* routes
    # past the supervision gate and exercise book resolution.
    launcher = AgentLauncher(tmp_path / "launch")
    app = create_dashboard_app(settings=settings, books=books, launcher=launcher)
    client = _client(app)
    yield SimpleNamespace(
        client=client, books=books, settings=settings, data_dir=data_dir, paths=paths
    )
    await client.aclose()
    for book in books:
        await book.storage.close()


class TestDiscovery:
    async def test_open_books_finds_per_mode_files(self, tmp_path) -> None:
        data_dir = tmp_path / "books"
        data_dir.mkdir()
        for mode, agent in [("demo", "crypto"), ("paper", "crypto"), ("paper", "stocks")]:
            path = str(db_layout.db_path(data_dir, mode, agent))
            await _seed_book(path, reasoning="x", total_value=1.0)
        settings = _settings(tmp_path, data_dir=str(data_dir))
        books = await open_books(settings)
        try:
            # Sorted agent then paper/demo/real; stocks book included.
            assert [b.key for b in books] == ["paper_crypto", "demo_crypto", "paper_stocks"]
            assert [b.mode for b in books] == ["paper", "demo", "paper"]
        finally:
            for b in books:
                await b.storage.close()

    async def test_legacy_shared_file_fallback(self, tmp_path) -> None:
        legacy = tmp_path / "trading_agent.db"
        seed = Storage(str(legacy))
        await seed.initialize()
        await seed.close()
        settings = _settings(tmp_path, database_path=str(legacy))
        books = await open_books(settings)
        try:
            assert [b.key for b in books] == ["crypto", "stocks"]  # pre-§7.78 keys
            assert all(b.mode is None for b in books)
            assert books[0].storage is books[1].storage  # one shared handle
        finally:
            for b in books:
                await b.storage.close()

    def test_find_book_ambiguous_agent_is_not_resolved(self) -> None:
        from src.dashboard.books import Book

        sentinel = object()
        books = [
            Book(key="paper_crypto", mode="paper", agent="crypto", storage=sentinel),
            Book(key="demo_crypto", mode="demo", agent="crypto", storage=sentinel),
        ]
        assert find_book(books, key=None, agent="crypto") is None  # ambiguous → nothing
        assert find_book(books, key="demo_crypto", agent=None).key == "demo_crypto"
        assert find_book(books, key=None, agent="demo_crypto").key == "demo_crypto"


class TestPages:
    async def test_overview_shows_a_card_per_book(self, books_env) -> None:
        body = (await books_env.client.get("/")).text
        assert ">paper_crypto<" in body and ">demo_crypto<" in body
        assert 'title="trading mode of this book' in body  # mode badges rendered
        # Filesystem paths must never leak into the HTML.
        assert str(books_env.data_dir) not in body

    async def test_portfolio_json_targets_one_book(self, books_env) -> None:
        demo = (await books_env.client.get("/api/portfolio.json?agent=demo_crypto")).json()
        assert demo["total_value"][-1] == pytest.approx(9_900.0)
        paper = (await books_env.client.get("/api/portfolio.json?book=paper_crypto")).json()
        assert paper["total_value"][-1] == pytest.approx(5_050.0)

    async def test_unknown_book_404_ambiguous_agent_404(self, books_env) -> None:
        assert (await books_env.client.get("/?agent=forex")).status_code == 404
        # Two crypto books → "?agent=crypto" must not silently pick one file.
        assert (await books_env.client.get("/?agent=crypto")).status_code == 404
        assert (await books_env.client.get("/config/crypto")).status_code == 404
        assert (await books_env.client.get("/config/demo_crypto")).status_code == 200

    async def test_decisions_merge_across_books(self, books_env) -> None:
        merged = (await books_env.client.get("/decisions")).text
        assert "paper-only-momentum" in merged and "demo-only-momentum" in merged
        only_demo = (await books_env.client.get("/decisions?agent=demo_crypto")).text
        assert "demo-only-momentum" in only_demo
        assert "paper-only-momentum" not in only_demo


class TestControlIsPerFile:
    async def test_pause_lands_only_in_that_book(self, books_env) -> None:
        resp = await books_env.client.post("/control/demo_crypto/pause")
        assert resp.status_code == 200

        demo = Storage(str(books_env.paths["demo"]), agent="crypto")
        paper = Storage(str(books_env.paths["paper"]), agent="crypto")
        await demo.initialize()
        await paper.initialize()
        try:
            demo_control = await demo.get_agent_control("crypto")
            paper_control = await paper.get_agent_control("crypto")
            assert demo_control is not None and demo_control.state == "paused"
            assert paper_control is None  # the other file never saw the latch
        finally:
            await demo.close()
            await paper.close()

    async def test_launch_refused_for_unknown_book(self, books_env) -> None:
        resp = await books_env.client.post("/launch/crypto/start")  # bare agent ≠ any key
        assert resp.status_code == 404
        # A known book whose process this dashboard never launched: refuse, no kill.
        resp = await books_env.client.post("/launch/paper_crypto/stop")
        assert resp.status_code == 409
