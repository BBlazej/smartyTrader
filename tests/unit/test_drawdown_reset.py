"""Drawdown peak re-baseline tests (§7.53): storage semantics + CLI planning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from scripts.rebaseline_drawdown import plan_rebaseline
from src.core.storage import Storage
from src.core.storage.models import PortfolioSnapshotRow


async def _backdate(storage: Storage, snapshot_id: int, when: datetime) -> None:
    async with await storage._session() as session:
        await session.execute(
            update(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.id == snapshot_id)
            .values(timestamp=when.astimezone(UTC).replace(tzinfo=None))
        )
        await session.commit()


@pytest.fixture()
async def storage(tmp_db_path: str) -> Storage:
    s = Storage(tmp_db_path)  # unbound like the CLI — every call names its agent
    await s.initialize()
    yield s
    await s.close()


class TestDrawdownResetStorage:
    async def test_record_and_get_round_trip(self, storage: Storage) -> None:
        assert await storage.get_drawdown_reset(agent="crypto") is None

        row = await storage.record_drawdown_reset(baseline_value=90_000.0, agent="crypto")
        assert row.agent == "crypto" and row.baseline_value == pytest.approx(90_000.0)

        fetched = await storage.get_drawdown_reset(agent="crypto")
        assert fetched is not None and fetched.baseline_value == pytest.approx(90_000.0)

    async def test_record_upserts_a_single_row_per_agent(self, storage: Storage) -> None:
        await storage.record_drawdown_reset(baseline_value=90_000.0, agent="crypto")
        second = await storage.record_drawdown_reset(baseline_value=85_000.0, agent="crypto")
        assert second.baseline_value == pytest.approx(85_000.0)

        again = await storage.get_drawdown_reset(agent="crypto")
        assert again is not None and again.baseline_value == pytest.approx(85_000.0)

    async def test_record_requires_an_explicit_agent(self, storage: Storage) -> None:
        with pytest.raises(ValueError):
            await storage.record_drawdown_reset(baseline_value=1.0)  # unbound → no scope

    async def test_effective_peak_without_reset_is_the_historical_max(
        self, storage: Storage
    ) -> None:
        await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=120.0, agent="crypto"
        )
        await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=200.0, agent="crypto"
        )
        assert await storage.get_effective_peak_equity(agent="crypto") == pytest.approx(200.0)

    async def test_reset_cuts_off_history_before_reset_at(self, storage: Storage) -> None:
        now = datetime.now(UTC)
        reset = await storage.record_drawdown_reset(baseline_value=85.0, agent="crypto")

        old = await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=200.0, agent="crypto"
        )
        await _backdate(storage, old, now - timedelta(days=1))  # predates the reset
        recent = await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=120.0, agent="crypto"
        )

        # The 200 from before the reset no longer latches; the post-reset 120 counts.
        assert await storage.get_effective_peak_equity(agent="crypto") == pytest.approx(120.0)

        # With nothing after reset_at, the recorded baseline is the floor/seed.
        await _backdate(storage, recent, now - timedelta(days=2))
        assert await storage.get_effective_peak_equity(agent="crypto") == pytest.approx(85.0)
        assert reset.agent == "crypto"

    async def test_reset_is_agent_scoped(self, storage: Storage) -> None:
        await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=300.0, agent="stocks"
        )
        await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=200.0, agent="crypto"
        )
        await storage.record_drawdown_reset(baseline_value=50.0, agent="crypto")

        assert await storage.get_effective_peak_equity(agent="crypto") == pytest.approx(50.0)
        # stocks never reset → its own history still latches untouched (§7.39 scoping).
        assert await storage.get_effective_peak_equity(agent="stocks") == pytest.approx(300.0)


class TestRebaselineCli:
    async def test_plan_defaults_to_latest_snapshot_value(self, storage: Storage) -> None:
        await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=98_000.0, agent="crypto"
        )
        await storage.save_portfolio_snapshot(
            cash=1, positions_json="[]", total_value=97_500.0, agent="crypto"
        )

        old_seed, new_baseline = await plan_rebaseline(storage, "crypto")

        assert old_seed == pytest.approx(98_000.0)
        assert new_baseline == pytest.approx(97_500.0)  # latest equity, not the peak
        assert await storage.get_drawdown_reset(agent="crypto") is None  # plan never writes

    async def test_plan_explicit_value_and_validation(self, storage: Storage) -> None:
        old_seed, new_baseline = await plan_rebaseline(storage, "crypto", value=90_000.0)
        assert old_seed is None and new_baseline == pytest.approx(90_000.0)  # no history needed

        with pytest.raises(SystemExit):
            await plan_rebaseline(storage, "stocks")  # no snapshots, no --value
        with pytest.raises(SystemExit):
            await plan_rebaseline(storage, "crypto", value=0.0)
        with pytest.raises(SystemExit):
            await plan_rebaseline(storage, "crypto", value=-5.0)
