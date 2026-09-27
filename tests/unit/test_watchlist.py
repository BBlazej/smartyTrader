"""Watchlist manager + storage tests (§7.70).

The manager must cap dynamic symbols, expire them on TTL, never duplicate or
drop core/held symbols, and fail soft (raising :class:`WatchlistRefreshFailed`)
when a dependency misbehaves — never mutate the caller's traded set.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import Settings, WatchlistSettings
from src.core.models import OHLCV, Position
from src.core.runner import run_agent
from src.core.storage import Storage
from src.core.watchlist import WatchlistManager, WatchlistRefreshFailed

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class FakeSnapshot:
    def __init__(self, symbol: str, candles: list[OHLCV]) -> None:
        self.symbol = symbol
        self.candles = candles


class FakeProvider:
    """Ticker volumes + deterministic daily candles per symbol."""

    def __init__(
        self,
        volumes: dict[str, float],
        drifts: dict[str, float] | None = None,
        depth: int = 30,
    ) -> None:
        self._volumes = volumes
        self._drifts = drifts or {}
        self._depth = depth
        self.snapshot_calls: list[str] = []

    async def fetch_quote_volumes(self, quote_currency: str | None = None) -> dict[str, float]:
        if quote_currency:
            suffix = f"/{quote_currency.upper()}"
            return {s: v for s, v in self._volumes.items() if s.endswith(suffix)}
        return dict(self._volumes)

    async def fetch_snapshot(self, symbol: str, timeframe: str = "1d") -> FakeSnapshot:
        self.snapshot_calls.append(symbol)
        drift = self._drifts.get(symbol, 0.02)
        closes: list[float] = []
        price = 100.0
        for day in range(self._depth):
            # Deterministic alternating noise so volatility is realistic but stable.
            price *= (1 + drift) if day % 2 == 0 else (1 - 0.005)
            closes.append(price)
        candles = [
            OHLCV(open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1_000_000.0) for c in closes
        ]
        return FakeSnapshot(symbol, candles)


def _config(**overrides: object) -> WatchlistSettings:
    base: dict[str, object] = {
        "enabled": True,
        "max_dynamic_symbols": 2,
        "ttl_hours": 48.0,
        "min_quote_volume_24h": 1_000_000.0,
        "momentum_days": 5,
        "lookback_days": 20,
        "min_daily_volatility": 0.0,
        "max_daily_volatility": None,
    }
    base.update(overrides)
    return WatchlistSettings(**base)  # type: ignore[arg-type]


def _manager(provider: FakeProvider, storage: Storage, **overrides: object) -> WatchlistManager:
    return WatchlistManager(
        provider=provider,
        storage=storage,
        config=_config(**overrides),
        component="crypto",
        quote_currency="EUR",
    )


@pytest.fixture()
async def storage(tmp_path):
    store = Storage(str(tmp_path / "watchlist.db"), agent="crypto")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


class TestWatchlistStorage:
    async def test_upsert_get_and_expire(self, storage: Storage) -> None:
        await storage.upsert_watchlist_entry("BTC/EUR", NOW + timedelta(hours=10), meta={"rank": 1})
        active = await storage.get_active_watchlist(now=NOW)
        assert [row.symbol for row in active] == ["BTC/EUR"]
        assert json.loads(active[0].meta_json)["rank"] == 1

        deleted = await storage.delete_expired_watchlist_entries(now=NOW + timedelta(hours=11))
        assert deleted == 1
        assert await storage.get_active_watchlist(now=NOW + timedelta(hours=11)) == []

    async def test_upsert_refreshes_existing_entry_not_duplicates(self, storage: Storage) -> None:
        await storage.upsert_watchlist_entry("BTC/EUR", NOW + timedelta(hours=10))
        await storage.upsert_watchlist_entry("BTC/EUR", NOW + timedelta(hours=30), meta={"rank": 2})
        active = await storage.get_active_watchlist(now=NOW)
        assert len(active) == 1
        assert json.loads(active[0].meta_json)["rank"] == 2

    async def test_entries_are_agent_scoped(self, storage: Storage) -> None:
        # An unbound view sees both; each agent binding sees only its own (§7.39 pattern).
        await storage.upsert_watchlist_entry("BTC/EUR", NOW + timedelta(hours=1))
        await storage.upsert_watchlist_entry("AAPL", NOW + timedelta(hours=1), agent="stocks")
        crypto_only = await storage.get_active_watchlist(agent="crypto", now=NOW)
        stocks_only = await storage.get_active_watchlist(agent="stocks", now=NOW)
        assert [row.symbol for row in crypto_only] == ["BTC/EUR"]
        assert [row.symbol for row in stocks_only] == ["AAPL"]


class TestWatchlistManager:
    async def test_refresh_adds_top_momentum_candidates_up_to_cap(self, storage: Storage) -> None:
        provider = FakeProvider(
            volumes={
                "BTC/EUR": 9e6,
                "ETH/EUR": 8e6,
                "XLM/EUR": 7e6,
                "THIN/EUR": 500_000.0,  # below the liquidity floor
            },
            drifts={"BTC/EUR": 0.01, "ETH/EUR": 0.05, "XLM/EUR": 0.03},
        )
        result = await _manager(provider, storage).refresh(["SOL/EUR"], held_symbols=[], now=NOW)
        # Momentum rank: ETH > XLM > BTC; cap is 2.
        assert result.added == ["ETH/EUR", "XLM/EUR"]
        assert result.symbols == ["SOL/EUR", "ETH/EUR", "XLM/EUR"]
        assert "THIN/EUR" not in provider.snapshot_calls  # floor applied before candle fetch

    async def test_core_symbols_are_never_added_or_duplicated(self, storage: Storage) -> None:
        provider = FakeProvider(volumes={"BTC/EUR": 9e6}, drifts={"BTC/EUR": 0.05})
        result = await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)
        assert result.added == []
        assert result.symbols == ["BTC/EUR"]

    async def test_held_symbols_stay_even_without_core_or_entry(self, storage: Storage) -> None:
        provider = FakeProvider(volumes={})
        result = await _manager(provider, storage).refresh(
            ["BTC/EUR"], held_symbols=["DOGE/EUR"], now=NOW
        )
        assert result.symbols == ["BTC/EUR", "DOGE/EUR"]

    async def test_expired_entries_deleted_and_slot_refilled(self, storage: Storage) -> None:
        await storage.upsert_watchlist_entry("OLD/EUR", NOW - timedelta(minutes=1))
        provider = FakeProvider(volumes={"NEW/EUR": 9e6}, drifts={"NEW/EUR": 0.05})
        result = await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)
        assert result.expired_deleted == 1
        assert "OLD/EUR" not in result.symbols
        assert "NEW/EUR" in result.added

    async def test_active_entry_is_not_readded_within_ttl(self, storage: Storage) -> None:
        await storage.upsert_watchlist_entry("ETH/EUR", NOW + timedelta(hours=10))
        provider = FakeProvider(volumes={"ETH/EUR": 9e6}, drifts={"ETH/EUR": 0.05})
        result = await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)
        assert result.added == []
        assert result.symbols.count("ETH/EUR") == 1

    async def test_cap_counts_existing_active_entries(self, storage: Storage) -> None:
        await storage.upsert_watchlist_entry("SEEN/EUR", NOW + timedelta(hours=10))
        provider = FakeProvider(
            volumes={"A/EUR": 9e6, "B/EUR": 8e6}, drifts={"A/EUR": 0.05, "B/EUR": 0.04}
        )
        result = await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)
        # Cap 2 with one slot already used by SEEN/EUR ⇒ only one new addition.
        assert result.added == ["A/EUR"]
        assert len(result.dynamic) == 2

    async def test_volatility_band_excludes_candidates(self, storage: Storage) -> None:
        provider = FakeProvider(volumes={"FLAT/EUR": 9e6}, drifts={"FLAT/EUR": 0.0})
        # Zero-drift candles ⇒ ~0.25 % alternating noise σ; demand more than that.
        result = await _manager(provider, storage, min_daily_volatility=0.5).refresh(
            ["BTC/EUR"], now=NOW
        )
        assert result.added == []

    async def test_exclude_symbols_never_added(self, storage: Storage) -> None:
        provider = FakeProvider(volumes={"WBTC/EUR": 9e6}, drifts={"WBTC/EUR": 0.05})
        result = await _manager(provider, storage, exclude_symbols=["wbtc/eur"]).refresh(
            ["BTC/EUR"], now=NOW
        )
        assert result.added == []

    async def test_missing_volume_support_fails_refresh(self, storage: Storage) -> None:
        class NoTickers(FakeProvider):
            fetch_quote_volumes = None  # type: ignore[assignment]

        provider = NoTickers(volumes={})
        with pytest.raises(WatchlistRefreshFailed, match="fetch_quote_volumes"):
            await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)

    async def test_volume_provider_error_fails_refresh_not_symbols(self, storage: Storage) -> None:
        class Broken(FakeProvider):
            async def fetch_quote_volumes(self, quote_currency=None):  # type: ignore[override,no-redef]
                raise TimeoutError("venue unreachable")

        with pytest.raises(WatchlistRefreshFailed, match="screener pass failed"):
            await _manager(Broken({}), storage).refresh(["BTC/EUR"], now=NOW)

    async def test_one_candidate_candle_failure_never_kills_the_pass(
        self, storage: Storage
    ) -> None:
        class HalfBroken(FakeProvider):
            async def fetch_snapshot(self, symbol: str, timeframe: str = "1d"):  # type: ignore[override]
                if symbol == "BAD/EUR":
                    raise TimeoutError("candle fetch failed")
                return await super().fetch_snapshot(symbol, timeframe)

        provider = HalfBroken(volumes={"BAD/EUR": 9e6, "GOOD/EUR": 8e6}, drifts={"GOOD/EUR": 0.05})
        result = await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)
        assert result.added == ["GOOD/EUR"]

    async def test_meta_records_ranking_inputs(self, storage: Storage) -> None:
        provider = FakeProvider(volumes={"ETH/EUR": 9e6}, drifts={"ETH/EUR": 0.05})
        await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)
        rows = await storage.get_active_watchlist(now=NOW)
        meta = json.loads(rows[0].meta_json)
        assert {"quote_volume_24h", "rank", "momentum", "daily_volatility"} <= meta.keys()

    async def test_forming_daily_bar_is_excluded_from_metrics(self, storage: Storage) -> None:
        class Timestamped(FakeProvider):
            async def fetch_snapshot(self, symbol: str, timeframe: str = "1d"):  # type: ignore[override]
                snapshot = await super().fetch_snapshot(symbol, timeframe)
                today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
                last = len(snapshot.candles) - 1
                for index, candle in enumerate(snapshot.candles):
                    candle.timestamp = today - timedelta(days=last - index)
                # Today's bar is still forming: a partial volume and a crashed print.
                snapshot.candles[-1].volume = 1.0
                snapshot.candles[-1].close = 1.0
                return snapshot

        provider = Timestamped(volumes={"ETH/EUR": 9e6}, drifts={"ETH/EUR": 0.05})
        await _manager(provider, storage).refresh(["BTC/EUR"], now=NOW)
        meta = json.loads((await storage.get_active_watchlist(now=NOW))[0].meta_json)
        # Closed bars only (§7.56): flat volume ⇒ no spike, uptrend ⇒ positive momentum.
        assert meta["volume_spike"] == pytest.approx(1.0)
        assert meta["momentum"] > 0


# ── Runner integration (§7.70) ────────────────────────────────

_WATCHLIST_YAML = """
llm: {{endpoint: "http://localhost:1234/v1/chat/completions", model: m}}
crypto_agent:
  enabled: true
  interval_minutes: 5
  pairs: ["BTC/EUR"]
  decision_history_limit: 10
  quote_currency: EUR
  watchlist:
    enabled: {enabled}
    max_dynamic_symbols: 2
    ttl_hours: 48
    min_quote_volume_24h: 1000000
    momentum_days: 5
    lookback_days: 20
    min_daily_volatility: 0.0
    max_daily_volatility: null
stocks_agent: {{enabled: false, interval_minutes: 60, symbols: ["AAPL"], decision_history_limit: 10}}
risk: {{max_position_pct: 0.1, daily_loss_limit_pct: 0.02, max_drawdown_pct: 0.05, consecutive_losses_cooldown_minutes: 60, max_open_positions: 5, min_confidence: 0.6}}
storage: {{database_path: "{db}"}}
monitoring: {{log_level: INFO}}
"""


class _RunnerProvider(FakeProvider):
    """Fake provider with the runner's lifecycle surface."""

    async def close(self) -> None:
        return None


class _RecordingAgent:
    def __init__(self) -> None:
        self.symbol_updates: list[list[str]] = []
        self.cycles = 0
        self._symbols: list[str] = ["BTC/EUR"]

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbol_updates.append(list(symbols))
        self._symbols = symbols

    async def run_cycle(self):
        # The traded set at cycle time must already include the dynamic symbols.
        self.cycles += 1
        return []

    async def start(self) -> None:  # pragma: no cover - scheduled path only
        return None

    async def shutdown(self) -> None:
        return None


class _OverrideSavingAgent(_RecordingAgent):
    """Saves an unrelated safe-config override mid-cycle, as the dashboard would."""

    def __init__(self) -> None:
        super().__init__()
        self._applier = None

    def set_control_overrides_applier(self, applier) -> None:  # type: ignore[no-untyped-def]
        self._applier = applier

    async def run_cycle(self):
        assert self._applier is not None
        self._applier('{"decision_history_limit": 5}')
        return await super().run_cycle()


async def _run_once_with_watchlist(
    tmp_path: Path,
    *,
    enabled: bool,
    positions: list[Position] | None = None,
    agent: _RecordingAgent | None = None,
) -> tuple[_RecordingAgent, Settings]:
    db = tmp_path / "runner.db"
    config = tmp_path / "settings.yaml"
    config.write_text(_WATCHLIST_YAML.format(enabled=str(enabled).lower(), db=db))
    settings = Settings(str(config))
    agent = agent or _RecordingAgent()

    provider = _RunnerProvider(
        volumes={"ETH/EUR": 9e6, "XLM/EUR": 8e6}, drifts={"ETH/EUR": 0.05, "XLM/EUR": 0.03}
    )
    executor = MagicMock()
    executor.close = AsyncMock()
    executor.get_positions = AsyncMock(return_value=positions or [])

    with (
        patch("src.core.runner.DecisionPipeline"),
        patch("src.core.runner.rehydrate_from_storage", new=AsyncMock()),
        patch("src.core.runner.prune_storage", new=AsyncMock()),
    ):
        await run_agent(
            settings,
            component="crypto",
            agent_enabled=True,
            interval_minutes=5,
            decision_history_limit=10,
            job_id="crypto_cycle",
            build_components=lambda: (provider, executor),
            build_agent=lambda pipeline, storage, risk_engine, llm_client: agent,
            run_once=True,
        )
    return agent, settings


class TestRunnerWatchlist:
    async def test_enabled_watchlist_widens_symbols_before_first_cycle(
        self, tmp_path: Path
    ) -> None:
        agent, _ = await _run_once_with_watchlist(tmp_path, enabled=True)
        # Cap is 2 and both candidates clear every filter → both join the core.
        assert agent.symbol_updates == [["BTC/EUR", "ETH/EUR", "XLM/EUR"]]
        assert agent.cycles == 1

    async def test_disabled_watchlist_touches_nothing(self, tmp_path: Path) -> None:
        agent, _ = await _run_once_with_watchlist(tmp_path, enabled=False)
        assert agent.symbol_updates == []
        assert agent.cycles == 1

    async def test_held_symbol_survives_even_if_not_traded_or_core(self, tmp_path: Path) -> None:
        held = Position(symbol="DOGE/EUR", quantity=10, avg_entry_price=0.1, current_price=0.2)
        agent, _ = await _run_once_with_watchlist(tmp_path, enabled=True, positions=[held])
        assert agent.symbol_updates == [["BTC/EUR", "ETH/EUR", "XLM/EUR", "DOGE/EUR"]]

    async def test_override_save_keeps_dynamic_and_held_symbols(self, tmp_path: Path) -> None:
        # parse_and_apply resets the agent to the core list on any override change;
        # the runner must re-merge the watchlist extras or a held dynamic symbol
        # would lose marking + exit enforcement until the next refresh (hours).
        held = Position(symbol="DOGE/EUR", quantity=10, avg_entry_price=0.1, current_price=0.2)
        agent, _ = await _run_once_with_watchlist(
            tmp_path, enabled=True, positions=[held], agent=_OverrideSavingAgent()
        )
        assert agent._symbols == ["BTC/EUR", "ETH/EUR", "XLM/EUR", "DOGE/EUR"]
