"""Screener-driven dynamic watchlist (§7.70, CHANGE.md §4.4 crypto-first).

The **core** symbol list (YAML ``pairs``/``symbols``, plus any safe-config override)
always stays in the traded set. On top of it the manager adds at most
``max_dynamic_symbols`` candidates chosen by the deterministic screener
(:mod:`src.analysis.screener`), each with a TTL; expired entries are deleted on
the next refresh and their slot frees up. Everything is audited: entries persist
in ``watchlist_entries`` with the ranking inputs they were added on.

Two safety invariants, both deterministic (project rule — universe control is
code, never model judgement):

* **Held symbols are never dropped.** A symbol with an open position stays in
  the effective list regardless of TTL/ranking — removing it would strand the
  position outside every cycle's marking and exit-level enforcement. If the
  position read fails, the whole refresh aborts: dropping a possibly-held
  symbol on a transient error is the one outcome worse than stale symbols.
* **A failed refresh never touches the traded set.** Provider/storage errors
  are surfaced as :class:`WatchlistRefreshFailed` for the caller to log —
  fail-soft, exactly like the other runner background jobs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import BaseModel, Field

from ..analysis.candles import split_forming
from ..analysis.screener import (
    ScreenedSymbol,
    compute_screen_metrics,
    filter_by_liquidity,
    rank_candidates,
)
from .config import WatchlistSettings

logger = structlog.get_logger()

#: Timeframe the screener reads daily candles on.
SCREENER_TIMEFRAME = "1d"


class WatchlistRefreshFailed(RuntimeError):
    """The refresh aborted; the caller must leave the traded set untouched."""


class WatchlistRefreshResult(BaseModel):
    """Outcome of one refresh — returned to the runner, logged for audit."""

    symbols: list[str]  # effective traded set (core + dynamic + held)
    dynamic: list[str] = Field(default_factory=list)  # active manager-added symbols
    added: list[str] = Field(default_factory=list)  # entries created on this pass
    expired_deleted: int = 0
    candidates_evaluated: int = 0


class WatchlistManager:
    """Maintains the capped, TTL'd dynamic symbol set for one agent.

    Constructed by ``run_agent`` only when ``<agent>.watchlist.enabled``; the
    provider must expose ``fetch_quote_volumes(quote_currency)`` and
    ``fetch_snapshot(symbol, timeframe)`` (CCXTProvider does). Storage calls pass
    the component explicitly so an unbound Storage still scopes rows correctly.
    """

    def __init__(
        self,
        *,
        provider: object,
        storage: object,
        config: WatchlistSettings,
        component: str,
        quote_currency: str | None = None,
        tradable_symbols: Callable[[], Awaitable[set[str]]] | None = None,
    ) -> None:
        self._provider = provider
        self._storage = storage
        self._config = config
        self._component = component
        self._quote_currency = quote_currency
        # Venue whitelist (CHANGE.md P4: only symbols the executor can actually trade).
        # Market data comes from the live venue while a demo account may list far
        # fewer pairs (OKX EEA demo: 29 EUR spot vs 243 live). ``None`` = no limit
        # (paper can trade anything with data). A failing lookup fails the refresh.
        self._tradable_symbols = tradable_symbols

    async def refresh(
        self,
        core_symbols: list[str],
        held_symbols: Iterable[str] = (),
        now: datetime | None = None,
    ) -> WatchlistRefreshResult:
        """Run one screener pass and return the effective traded symbol set.

        Raises :class:`WatchlistRefreshFailed` when a dependency misbehaves — the
        caller must then keep its current symbol list untouched.
        """
        moment = now or datetime.now(UTC)
        core = list(dict.fromkeys(core_symbols))  # dedupe, keep order
        held = [s for s in dict.fromkeys(held_symbols)]

        try:
            expired_deleted = await self._storage.delete_expired_watchlist_entries(  # type: ignore[attr-defined]
                agent=self._component, now=moment
            )
            active_rows = await self._storage.get_active_watchlist(  # type: ignore[attr-defined]
                agent=self._component, now=moment
            )
        except Exception as exc:
            raise WatchlistRefreshFailed(f"watchlist storage read failed: {exc}") from exc

        dynamic = [row.symbol for row in active_rows]
        added: list[str] = []
        evaluated = 0

        try:
            ranked = await self._screen(core, held, moment)
        except WatchlistRefreshFailed:
            raise
        except Exception as exc:
            raise WatchlistRefreshFailed(f"screener pass failed: {exc}") from exc

        evaluated = len(ranked)
        slots = self._config.max_dynamic_symbols - len(dynamic)
        if slots > 0 and ranked:
            expires_at = moment + timedelta(hours=self._config.ttl_hours)
            try:
                for candidate in ranked:
                    if slots <= 0:
                        break
                    if candidate.symbol in core or candidate.symbol in dynamic:
                        continue
                    await self._storage.upsert_watchlist_entry(  # type: ignore[attr-defined]
                        candidate.symbol,
                        expires_at,
                        agent=self._component,
                        source="screener",
                        meta={
                            "quote_volume_24h": candidate.quote_volume_24h,
                            "rank": candidate.rank,
                            "momentum": candidate.metrics.momentum,
                            "daily_volatility": candidate.metrics.daily_volatility,
                            "volume_spike": candidate.metrics.volume_spike,
                        },
                    )
                    dynamic.append(candidate.symbol)
                    added.append(candidate.symbol)
                    slots -= 1
            except Exception as exc:
                raise WatchlistRefreshFailed(f"watchlist entry write failed: {exc}") from exc

        symbols = list(dict.fromkeys(core + dynamic + held))
        result = WatchlistRefreshResult(
            symbols=symbols,
            dynamic=dynamic,
            added=added,
            expired_deleted=int(expired_deleted),
            candidates_evaluated=evaluated,
        )
        logger.info(
            "watchlist refreshed",
            component=self._component,
            core=len(core),
            dynamic=result.dynamic,
            added=result.added,
            expired_deleted=result.expired_deleted,
            candidates_evaluated=result.candidates_evaluated,
            symbols=symbols,
        )
        return result

    # ── Screener pass ────────────────────────────────────────

    async def _screen(
        self, core: list[str], held: list[str], now: datetime
    ) -> list[ScreenedSymbol]:
        """Liquidity floor → candle metrics → volatility band → momentum rank."""
        fetch_volumes = getattr(self._provider, "fetch_quote_volumes", None)
        if fetch_volumes is None:
            raise WatchlistRefreshFailed(
                "provider has no fetch_quote_volumes — the screener needs a ticker-capable feed"
            )
        volumes = await fetch_volumes(self._quote_currency)
        if not isinstance(volumes, dict):
            raise WatchlistRefreshFailed("fetch_quote_volumes did not return a mapping")

        cfg = self._config
        excluded = {s.upper() for s in (*cfg.exclude_symbols, *core, *held)}
        tradable: set[str] | None = None
        if self._tradable_symbols is not None:
            tradable = {s.upper() for s in await self._tradable_symbols()}
        liquid = [
            (symbol, volume)
            for symbol, volume in filter_by_liquidity(volumes, cfg.min_quote_volume_24h)
            if symbol.upper() not in excluded and (tradable is None or symbol.upper() in tradable)
        ][: cfg.max_candidates]

        fetch_snapshot = getattr(self._provider, "fetch_snapshot", None)
        if fetch_snapshot is None:
            raise WatchlistRefreshFailed("provider has no fetch_snapshot — cannot rank candidates")

        triples = []
        for symbol, volume in liquid:
            try:
                snapshot = await fetch_snapshot(symbol, SCREENER_TIMEFRAME)
                # Closed bars only (§7.56): today's forming bar carries a partial
                # volume and a moving close — it would skew the spike and momentum.
                closed, _ = split_forming(snapshot.candles, SCREENER_TIMEFRAME, now)
                metrics = compute_screen_metrics(closed, cfg.momentum_days, cfg.lookback_days)
            except Exception as exc:  # noqa: BLE001 - one bad candidate never kills the pass
                logger.debug(
                    "screener candidate skipped",
                    symbol=symbol,
                    component=self._component,
                    error=str(exc),
                )
                continue
            if metrics is None:
                logger.debug(
                    "screener candidate skipped (candle depth below minimum)",
                    symbol=symbol,
                    component=self._component,
                )
                continue
            triples.append((symbol, volume, metrics))

        return rank_candidates(
            triples,
            min_daily_volatility=cfg.min_daily_volatility,
            max_daily_volatility=cfg.max_daily_volatility,
        )
