"""Agent control plane persistence (storage mixin, §7.36 / §7.15).

The DB is the control source of truth: one ``agent_control`` row per agent holds the
state/close-all latch, heartbeat, and safe-config overrides.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import AgentControlRow


class ControlMixin:
    """``agent_control`` reads/writes — latches, heartbeat, config overrides."""

    async def get_agent_control(self, agent: str) -> AgentControlRow | None:
        """Fetch the control row for ``agent`` (``None`` = never touched, use defaults)."""
        async with await self._session() as session:
            return await session.get(AgentControlRow, agent)

    async def _upsert_agent_control(self, agent: str, **values: object) -> AgentControlRow:
        """Create the control row on first write, then patch the given columns."""
        async with await self._session() as session:
            row = await session.get(AgentControlRow, agent)
            if row is None:
                row = AgentControlRow(agent=agent)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()
            return row

    async def set_agent_state(self, agent: str, state: str) -> AgentControlRow:
        """Pause/resume an agent. The agent picks the change up on its next cycle."""
        if state not in ("running", "paused"):
            raise ValueError(f"invalid agent state: {state!r}")
        return await self._upsert_agent_control(agent, state=state)

    async def request_close_all(self, agent: str, requested: bool = True) -> AgentControlRow:
        """Set/clear the close-all latch; the agent closes every position then clears it."""
        return await self._upsert_agent_control(agent, close_all_requested=requested)

    async def record_cycle_health(
        self, agent: str, last_error: str | None = None
    ) -> AgentControlRow:
        """Heartbeat after a cycle: stamp ``last_cycle_at`` and the latest error."""
        return await self._upsert_agent_control(
            agent,
            last_cycle_at=datetime.now(UTC).replace(tzinfo=None),
            last_error=last_error,
        )

    async def set_config_override(self, agent: str, overrides_json: str | None) -> AgentControlRow:
        """Persist the whitelist-validated safe-config overrides (``None`` clears them)."""
        return await self._upsert_agent_control(agent, config_override_json=overrides_json)
