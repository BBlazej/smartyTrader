"""LLM decision persistence (storage mixin, §7.36).

Includes the outcome-attribution helpers (``set_realized_pnl``/``add_realized_pnl``,
fail-soft) and the queries feeding prompt context, cooldown rehydration, and the
decision-replay backtester.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy import select

from .models import LLMDecisionRow, _as_naive_utc


class DecisionMixin:
    """``llm_decisions`` writes/reads."""

    async def save_llm_decision(
        self,
        symbol: str,
        action: str,
        confidence: float,
        reasoning: str,
        stop_loss: float | None,
        take_profit: float | None,
        risk_verdict: str,
        risk_reason: str | None,
        realized_pnl: float | None = None,
        is_fallback: bool = False,
        agent: str | None = None,
        llm_latency_ms: float | None = None,
        llm_prompt_tokens: int | None = None,
        llm_completion_tokens: int | None = None,
    ) -> int:
        async with await self._session() as session:
            row = LLMDecisionRow(
                symbol=symbol,
                action=action,
                confidence=confidence,
                reasoning=reasoning,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_verdict=risk_verdict,
                risk_reason=risk_reason,
                realized_pnl=realized_pnl,
                is_fallback=is_fallback,
                llm_latency_ms=llm_latency_ms,
                llm_prompt_tokens=llm_prompt_tokens,
                llm_completion_tokens=llm_completion_tokens,
                agent=self._agent_scope(agent),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def set_realized_pnl(self, decision_id: int, realized_pnl: float) -> None:
        """Stamp the net realized PnL onto a decision once its position closed.

        Called by the agent when an order realizes PnL, so the decision row
        carries the *outcome* the LLM is later shown (the "learn from its track
        record" loop). Fails soft — an outcome-recording error must not break
        a trading cycle.
        """
        from sqlalchemy import update

        log = structlog.get_logger()
        try:
            async with await self._session() as session:
                await session.execute(
                    update(LLMDecisionRow)
                    .where(LLMDecisionRow.id == decision_id)
                    .values(realized_pnl=realized_pnl)
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to record realized pnl", decision_id=decision_id, error=str(exc))

    async def add_realized_pnl(self, decision_id: int, delta: float) -> None:
        """Accumulate realized PnL onto a (typically the *entry*) decision (§7.8).

        A position closed in several tranches must sum its shares onto the same
        opening decision; unlike :meth:`set_realized_pnl` this adds to any
        existing value instead of overwriting. Fail-soft like ``set_realized_pnl``.
        """
        from sqlalchemy import func, update

        log = structlog.get_logger()
        try:
            async with await self._session() as session:
                await session.execute(
                    update(LLMDecisionRow)
                    .where(LLMDecisionRow.id == decision_id)
                    .values(realized_pnl=func.coalesce(LLMDecisionRow.realized_pnl, 0.0) + delta)
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to accumulate realized pnl", decision_id=decision_id, error=str(exc)
            )

    async def get_exit_levels(
        self, decision_ids: list[int]
    ) -> dict[int, tuple[float | None, float | None]]:
        """``decision_id → (stop_loss, take_profit)`` for the given decisions (§7.58).

        The entry signal's levels are what the pipeline passed to ``place_order``, so
        venue executors re-arm local SL/TP enforcement from them after a restart.
        Decision ids are globally unique, so no agent scope is needed here.
        """
        if not decision_ids:
            return {}
        async with await self._session() as session:
            stmt = select(
                LLMDecisionRow.id, LLMDecisionRow.stop_loss, LLMDecisionRow.take_profit
            ).where(LLMDecisionRow.id.in_(set(decision_ids)))
            result = await session.execute(stmt)
            return {row.id: (row.stop_loss, row.take_profit) for row in result.all()}

    async def get_closed_decisions(
        self, limit: int = 50, agent: str | None = None
    ) -> list[LLMDecisionRow]:
        """Most recent decisions that carry a realized outcome (``realized_pnl`` set).

        Used at startup to rehydrate the consecutive-loss/cooldown trackers (§7.7);
        agent-scoped (§7.39) so one agent's losses never arm the other's cooldown.
        """
        async with await self._session() as session:
            stmt = select(LLMDecisionRow).where(LLMDecisionRow.realized_pnl.isnot(None))
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(LLMDecisionRow.agent == scope)
            stmt = stmt.order_by(LLMDecisionRow.timestamp.desc(), LLMDecisionRow.id.desc()).limit(
                limit
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_recent_decisions(
        self,
        symbol: str | None = None,
        limit: int = 10,
        include_fallback: bool = False,
        agent: str | None = None,
    ) -> list[LLMDecisionRow]:
        """Return the most recent decisions, most-recent first.

        Each row now carries its ``realized_pnl`` outcome (None while the
        position is still open), which is surfaced to the LLM as context.
        ``include_fallback=True`` widens the view for audit surfaces (control API /
        dashboard); prompt context keeps using the default that excludes them (§7.8).
        """
        async with await self._session() as session:
            # LLM-unavailable fallback rows are audit-only context — never re-fed
            # to the model as if it had genuinely decided to HOLD (§7.8).
            stmt = select(LLMDecisionRow)
            if not include_fallback:
                stmt = stmt.where(LLMDecisionRow.is_fallback.isnot(True))
            stmt = stmt.order_by(LLMDecisionRow.timestamp.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(LLMDecisionRow.symbol == symbol)
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(LLMDecisionRow.agent == scope)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_decisions_in_range(
        self,
        start: datetime,
        end: datetime,
        symbols: list[str] | None = None,
        include_fallback: bool = False,
        agent: str | None = None,
    ) -> list[LLMDecisionRow]:
        """Decisions within ``[start, end]`` (UTC), oldest first (§7.14 replay input).

        LLM-fallback rows are excluded by default — they never produced a real
        trade decision (§7.8) and would only add noise to the replay.
        Timestamps are compared as naive UTC (SQLite has no tz-aware storage).
        """
        cutoff_start = _as_naive_utc(start)
        cutoff_end = _as_naive_utc(end)
        async with await self._session() as session:
            stmt = select(LLMDecisionRow).where(
                LLMDecisionRow.timestamp >= cutoff_start,
                LLMDecisionRow.timestamp <= cutoff_end,
            )
            if symbols:
                stmt = stmt.where(LLMDecisionRow.symbol.in_(symbols))
            if not include_fallback:
                stmt = stmt.where(LLMDecisionRow.is_fallback == False)
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(LLMDecisionRow.agent == scope)
            stmt = stmt.order_by(LLMDecisionRow.timestamp.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_llm_latency_stats(
        self, limit: int = 100, agent: str | None = None
    ) -> dict[str, float | int | None]:
        """Latency/token percentiles over the last ``limit`` LLM-timed decisions (§7.69).

        Feeds the dashboard's p50/p95 badges and CHANGE.md's watchlist sizing: how
        many symbols fit a decision interval is a function of p95 latency, so the
        numbers must come from real per-decision calls, not one-off benchmarks.
        Rows without timing (pre-§7.69 history) are ignored; ``count`` says how
        many samples back the percentiles.
        """
        async with await self._session() as session:
            stmt = (
                select(
                    LLMDecisionRow.llm_latency_ms,
                    LLMDecisionRow.llm_prompt_tokens,
                    LLMDecisionRow.llm_completion_tokens,
                )
                .where(LLMDecisionRow.llm_latency_ms.isnot(None))
                .order_by(LLMDecisionRow.timestamp.desc(), LLMDecisionRow.id.desc())
                .limit(limit)
            )
            scope = self._agent_scope(agent)
            if scope is not None:
                stmt = stmt.where(LLMDecisionRow.agent == scope)
            result = await session.execute(stmt)
            rows = list(result.all())

        latencies = sorted(float(r[0]) for r in rows if r[0] is not None)
        prompt = [int(r[1]) for r in rows if r[1] is not None]
        completion = [int(r[2]) for r in rows if r[2] is not None]

        def pct(values: list[float], q: float) -> float | None:
            if not values:
                return None
            idx = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
            return values[idx]

        return {
            "count": len(latencies),
            "p50_ms": pct(latencies, 0.50),
            "p95_ms": pct(latencies, 0.95),
            "max_ms": latencies[-1] if latencies else None,
            "avg_prompt_tokens": (sum(prompt) / len(prompt)) if prompt else None,
            "avg_completion_tokens": (sum(completion) / len(completion)) if completion else None,
        }
