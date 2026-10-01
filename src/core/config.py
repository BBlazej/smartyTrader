"""Configuration loading from YAML + environment variables.

Every block is a Pydantic model (``extra="forbid"`` — a misspelled key fails at load),
with the project's own rules as validators that keep their exact error messages.
A YAML ``null`` means "use the default" unless the field itself accepts ``None``.
"""

from __future__ import annotations

import os
import re
import types
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, Union, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..analysis.candles import timeframe_delta

#: Environment acknowledgement required, together with an explicit config flag,
#: before any executor may touch a REAL-money account (§7.41): a keyed exchange run
#: with ``testnet: false`` and ``xtb_execution.account_type: real``.
LIVE_TRADING_ACK_ENV = "LIVE_TRADING_ACK"

#: Env override for the LLM endpoint — any OpenAI-compatible server (LM Studio,
#: Unsloth desktop, llama-server, Ollama).
LLM_ENDPOINT_ENV = "LOCAL_LLM_ENDPOINT"
#: Env opt-in for strict JSON ``response_format``.
LLM_JSON_SCHEMA_ENV = "LOCAL_LLM_USE_JSON_SCHEMA"
LIVE_TRADING_ACK_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"

#: Event importance levels, lowest first (§7.18; mirrors ``models.EventImportance``).
IMPORTANCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: Prompt playbooks a sleeve or agent may use (§7.71) — texts live in
#: :data:`src.analysis.prompt_builder.PLAYBOOKS` (pinned equal by a test).
PLAYBOOK_NAMES: tuple[str, ...] = ("swing", "position", "test")

#: LLM fields a summarizer may override (§7.18) — everything else is the main block's.
_SUMMARIZER_LLM_FIELDS: frozenset[str] = frozenset(
    {
        "endpoint",
        "model",
        "timeout_seconds",
        "max_retries",
        "temperature",
        "max_tokens",
        "retry_backoff_base_seconds",
        "seed",
        "reasoning",
    }
)

#: ``llm.reasoning`` (§7.91): ``default`` sends nothing (the server's own behaviour),
#: ``off`` disables thinking, a level caps its effort.
ReasoningMode = Literal["default", "off", "low", "medium", "high", "xhigh"]

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
#: Sleeve names become the ``strategy`` column (String(20)) on decisions/orders.
_SLEEVE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,19}$")


def pairs_not_quoted_in(pairs: list[str], quote_currency: str | None) -> list[str]:
    """Pairs whose quote side is not ``quote_currency`` (none when no quote is set).

    Shared by startup validation and the control plane: a ``pairs`` override must
    obey the same rule as the YAML list (§7.28 smoke-run find — a stale USDT
    override silently replaced the EUR pairs on OKX Europe).
    """
    if not quote_currency:
        return []
    return [p for p in pairs if p.split("/")[-1].upper() != quote_currency.upper()]


def live_trading_acknowledged() -> bool:
    """True only when ``LIVE_TRADING_ACK`` holds the exact acknowledgement phrase."""
    return os.getenv(LIVE_TRADING_ACK_ENV, "").strip() == LIVE_TRADING_ACK_PHRASE


def _nullable(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return annotation is None or (
        origin in (Union, types.UnionType) and type(None) in get_args(annotation)
    )


def _positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0")


class _Config(BaseModel):
    """Base for every settings block: unknown keys fail, ``null`` → default."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _null_means_default(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fields = cls.model_fields
        return {
            key: value
            for key, value in data.items()
            if not (value is None and key in fields and not _nullable(fields[key].annotation))
        }


# ── LLM ───────────────────────────────────────────────────────


class LLMSettings(_Config):
    endpoint: str
    model: str
    timeout_seconds: int = 30
    max_retries: int = 3
    # Opt-in strict JSON ``response_format`` (or env LOCAL_LLM_USE_JSON_SCHEMA). Enable
    # once the local model supports it — some setups reject it, which would otherwise
    # force the safe HOLD fallback every cycle.
    use_json_schema: bool = False
    # Sampling knobs were hardcoded in the client ("config-driven" rule, §7.19).
    temperature: float = 0.2
    max_tokens: int = 1024
    # Base delay for the exponential backoff between retry attempts; 0 disables it.
    retry_backoff_base_seconds: float = 1.0
    # Deterministic-evaluation seed (§7.33): sent with every request when set;
    # None omits the field entirely — provider default behavior.
    seed: int | None = None
    # §7.91: how much the model thinks before answering — sent per request as Unsloth
    # Studio's ``enable_thinking`` / ``reasoning_effort`` (other servers ignore them).
    reasoning: ReasoningMode = "default"
    # §7.89: where to cancel an in-flight generation at shutdown — a path on the LLM
    # server (or a full URL). With it set, every request carries a fresh ``cancel_id``
    # and shutdown POSTs ``{"cancel_id": …}`` there (Unsloth Studio:
    # ``/api/inference/cancel``). None: closing the connection is the only signal
    # (llama-server and LM Studio stop a generation whose client disconnected).
    cancel_path: str | None = None
    # Bearer key for servers that require one (Unsloth desktop, llama-server
    # --api-key, vLLM). A secret: env ``LLM_API_KEY`` only — never YAML, never logged.
    api_key: str | None = Field(default=None, repr=False)

    @property
    def max_response_chars(self) -> int:
        """Raw completion size cap before parsing (§7.33): ~4 chars per token of
        ``max_tokens``, so a full-length answer always fits and a runaway one fails
        the attempt instead of reaching the parser. Derived (§7.91) — it can no longer
        drift out of step with ``max_tokens``."""
        return 4 * self.max_tokens

    @model_validator(mode="before")
    @classmethod
    def _key_from_env_only(cls, data: Any) -> Any:
        if isinstance(data, dict) and "api_key" in data:
            raise ValueError("llm.api_key is read from the LLM_API_KEY environment variable only")
        return data

    @model_validator(mode="after")
    def _apply_env(self) -> LLMSettings:
        self.endpoint = os.getenv(LLM_ENDPOINT_ENV, "").strip() or self.endpoint
        self.api_key = os.getenv("LLM_API_KEY", "").strip() or None
        self.use_json_schema = self.use_json_schema or os.getenv(
            LLM_JSON_SCHEMA_ENV, ""
        ).strip().lower() in ("1", "true", "yes")
        return self


def summarizer_llm_settings(base: LLMSettings, overrides: dict[str, Any]) -> LLMSettings:
    """The summarizer's LLM settings: the trading block with ``overrides`` applied (§7.18).

    A copy, so the endpoint/API key resolved from the environment carry over and
    nothing written here leaks back into the trading client.
    """
    return base.model_copy(update=overrides)


# ── Risk ──────────────────────────────────────────────────────


class RiskSettings(_Config):
    max_position_pct: float = 0.10
    daily_loss_limit_pct: float = 0.02
    max_drawdown_pct: float = 0.05
    consecutive_losses_cooldown_minutes: int = 60
    # Streak that arms the cooldown (§7.19).
    consecutive_losses_threshold: int = 3
    max_open_positions: int = 5
    min_confidence: float = 0.6
    # §7.54 entry geometry: a BUY's stop must sit BELOW the current price and within
    # this fraction of it — an SL at 0.01 means unbounded risk.
    max_stop_distance_pct: float = 0.25
    # §7.54 optional risk-per-trade sizing: (entry − stop) × quantity ≤ this fraction
    # of total value. 0 disables it.
    risk_per_trade_pct: float = 0.0
    # Deterministic stop-loss / take-profit enforcement (§7.9).
    enforce_exit_levels: bool = True
    # §7.18 event guard (entries only): no new BUY around a scheduled high-impact
    # macro event, around the symbol's earnings, or after a venue delisting notice.
    event_guard_enabled: bool = True
    event_blackout_before_minutes: int = 120
    event_blackout_after_minutes: int = 60
    event_guard_min_importance: str = "high"
    earnings_blackout_days_before: int = 1
    earnings_blackout_hours_after: int = 24
    delisting_blackout_days: int = 90

    @model_validator(mode="after")
    def _check(self) -> RiskSettings:
        for name in (
            "event_blackout_before_minutes",
            "event_blackout_after_minutes",
            "earnings_blackout_days_before",
            "earnings_blackout_hours_after",
            "delisting_blackout_days",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"risk.{name} must be >= 0")
        if self.event_guard_min_importance not in IMPORTANCE_LEVELS:
            raise ValueError(
                f"risk.event_guard_min_importance must be one of {', '.join(IMPORTANCE_LEVELS)}"
            )
        return self


# ── Screener watchlist (§7.70) ────────────────────────────────


class NewsMentionSettings(_Config):
    """News mentions as a watchlist priority (§7.83, CHANGE.md §4.4). Off by default."""

    enabled: bool = False
    lookback_hours: float = 48.0
    min_mentions: int = 2

    @model_validator(mode="after")
    def _check(self) -> NewsMentionSettings:
        if self.lookback_hours <= 0:
            raise ValueError("watchlist.news_mentions.lookback_hours must be > 0")
        if self.min_mentions < 1:
            raise ValueError("watchlist.news_mentions.min_mentions must be >= 1")
        return self


class WatchlistSettings(_Config):
    """Deterministic screener + capped watchlist manager (§7.70, CHANGE.md §4.4).

    All limits are hard-coded guards applied by code, never model judgement. Opt-in:
    with it off the agent trades exactly its YAML symbol list.
    """

    enabled: bool = False
    # How often the runner re-runs the screener while alive.
    refresh_minutes: int = 360
    # Hard cap on manager-added symbols (core YAML symbols never count).
    max_dynamic_symbols: int = 2
    # A dynamic symbol is dropped after this many hours unless re-added; symbols
    # with an open position are always kept.
    ttl_hours: float = 96.0
    # Liquidity floor: 24 h traded volume in quote currency (e.g. EUR).
    min_quote_volume_24h: float = 1_000_000.0
    # Momentum ranking window / candle lookback (daily bars).
    momentum_days: int = 14
    lookback_days: int = 30
    # Volatility band on daily returns: below the floor dead-flat, above the cap a
    # blow-up risk (``null`` = no cap).
    min_daily_volatility: float = 0.005
    max_daily_volatility: float | None = 0.25
    # Candle fetches per refresh are capped to this many best-liquidity candidates.
    max_candidates: int = 20
    # Symbols the manager must never add (stables, wrapped/leveraged tokens…).
    exclude_symbols: list[str] = Field(default_factory=list)
    # §7.83: candidates named in recent news move ahead (after every filter).
    news_mentions: NewsMentionSettings = Field(default_factory=NewsMentionSettings)

    @field_validator("exclude_symbols")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        return [s.upper() for s in value]

    @model_validator(mode="after")
    def _check(self) -> WatchlistSettings:
        if self.refresh_minutes < 1:
            raise ValueError("watchlist.refresh_minutes must be >= 1")
        if self.max_dynamic_symbols < 1:
            raise ValueError("watchlist.max_dynamic_symbols must be >= 1")
        if self.ttl_hours <= 0:
            raise ValueError("watchlist.ttl_hours must be > 0")
        if self.momentum_days < 1:
            raise ValueError("watchlist.momentum_days must be >= 1")
        if self.lookback_days < self.momentum_days + 2:
            raise ValueError(
                "watchlist.lookback_days must cover the momentum window plus two extra "
                "closes (lookback_days >= momentum_days + 2), otherwise metrics never compute"
            )
        if self.min_daily_volatility < 0:
            raise ValueError("watchlist.min_daily_volatility must be >= 0")
        if (
            self.max_daily_volatility is not None
            and self.max_daily_volatility < self.min_daily_volatility
        ):
            raise ValueError(
                "watchlist.max_daily_volatility must be >= watchlist.min_daily_volatility"
            )
        if self.max_candidates < 1:
            raise ValueError("watchlist.max_candidates must be >= 1")
        return self


# ── Market context (§7.18) ────────────────────────────────────


class SentimentSettings(_Config):
    """Market-wide sentiment feed. Shipped source: crypto Fear & Greed."""

    SOURCES: ClassVar[tuple[str, ...]] = ("fear_greed",)

    enabled: bool = False
    source: str = "fear_greed"
    url: str = "https://api.alternative.me/fng/?limit=2"
    # Older readings are not shown (the index updates daily).
    max_age_hours: float = 36.0

    @model_validator(mode="after")
    def _check(self) -> SentimentSettings:
        if self.source not in self.SOURCES:
            raise ValueError(f"context.sentiment.source must be one of {', '.join(self.SOURCES)}")
        _positive("context.sentiment.max_age_hours", self.max_age_hours)
        return self


class MacroContextSettings(_Config):
    """Which macro events (``macro_calendar``) this agent cares about."""

    enabled: bool = False
    # Economies whose releases move this market (USD CPI/FOMC move crypto too).
    currencies: list[str] = Field(default_factory=lambda: ["USD", "EUR"])
    # Events below this importance are not stored (nor shown, nor guarded).
    min_importance: str = "high"

    @model_validator(mode="after")
    def _check(self) -> MacroContextSettings:
        self.currencies = [c.upper() for c in self.currencies]
        bad = [c for c in self.currencies if not _CURRENCY_RE.match(c)]
        if bad:
            raise ValueError(f"context.macro.currencies: not ISO currency codes: {bad}")
        if self.min_importance not in IMPORTANCE_LEVELS:
            raise ValueError(
                f"context.macro.min_importance must be one of {', '.join(IMPORTANCE_LEVELS)}"
            )
        return self


class AnnouncementsSettings(_Config):
    """Venue announcements: OKX delisting notices, public API, no key."""

    enabled: bool = False
    # OKX Europe's host (§7.64); the global site serves the same endpoint.
    base_url: str = "https://eea.okx.com"
    # How far back a notice still matches a traded asset for the prompt.
    max_age_days: int = 120

    @model_validator(mode="after")
    def _check(self) -> AnnouncementsSettings:
        self.base_url = self.base_url.rstrip("/")
        _positive("context.announcements.max_age_days", self.max_age_days)
        return self


class EarningsSettings(_Config):
    """Stock earnings dates via yfinance (stocks agent)."""

    enabled: bool = False
    lookahead_days: int = 45

    @model_validator(mode="after")
    def _check(self) -> EarningsSettings:
        _positive("context.earnings.lookahead_days", self.lookahead_days)
        return self


class NewsFeedSpec(_Config):
    """One RSS/Atom feed. ``symbols`` pins every item to those symbols (e.g. an EDGAR
    per-company filings feed); otherwise items match by keyword aliases."""

    name: str
    url: str
    symbols: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> NewsFeedSpec:
        self.name = self.name.strip()
        if not self.name or len(self.name) > 40:
            raise ValueError("context.news.feeds[].name must be a non-empty string (<= 40 chars)")
        if not self.url.startswith(("https://", "http://")):
            raise ValueError(f"context.news.feeds.{self.name}.url must be an http(s) URL")
        return self


class NewsSettings(_Config):
    """RSS/Atom news + filings ingest. Raw text feeds only the summarizer."""

    enabled: bool = False
    feeds: list[NewsFeedSpec] = Field(default_factory=list)
    # Extra keywords per symbol (case-insensitive, whole word). The base asset /
    # ticker itself always matches, e.g. {"BTC/EUR": ["bitcoin"]}.
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    max_items_per_feed: int = 30
    max_item_chars: int = 2000
    # Older items are ignored at ingest.
    max_age_hours: float = 48.0
    # Response size cap per feed download.
    max_feed_bytes: int = 2_000_000

    @model_validator(mode="after")
    def _check(self) -> NewsSettings:
        if self.enabled and not self.feeds:
            raise ValueError("context.news.enabled needs at least one entry under feeds")
        for symbol, words in self.aliases.items():
            if not all(words):
                raise ValueError(f"context.news.aliases.{symbol} must be a list of strings")
        self.aliases = {
            symbol: [w.lower() for w in words] for symbol, words in self.aliases.items()
        }
        for name in ("max_items_per_feed", "max_item_chars", "max_age_hours", "max_feed_bytes"):
            _positive(f"context.news.{name}", getattr(self, name))
        return self


class SummarizerSettings(_Config):
    """Batch LLM summarizer → per-symbol context cards (CHANGE.md §4.4).

    Runs off the trade path on its own cadence and shares one in-process lock with the
    trading LLM client. ``llm`` overrides fields of the top-level ``llm:`` block (e.g. a
    smaller ``model`` — CHANGE.md Q7); unset → the trading model.
    """

    enabled: bool = False
    refresh_minutes: int = 240
    card_ttl_hours: float = 12.0
    # News window a card digests, and how many items it may read.
    lookback_hours: float = 24.0
    max_items_per_card: int = 8
    # At most this many symbols are summarized per pass (LLM budget, CHANGE.md §4.7).
    max_symbols_per_run: int = 5
    llm: dict[str, Any] = Field(default_factory=dict)

    @property
    def llm_overrides(self) -> dict[str, Any]:
        return self.llm

    @model_validator(mode="after")
    def _check(self) -> SummarizerSettings:
        bad = set(self.llm) - _SUMMARIZER_LLM_FIELDS
        if bad:
            raise ValueError(f"context.summarizer.llm: unknown or forbidden fields {sorted(bad)}")
        for name in (
            "refresh_minutes",
            "card_ttl_hours",
            "lookback_hours",
            "max_items_per_card",
            "max_symbols_per_run",
        ):
            _positive(f"context.summarizer.{name}", getattr(self, name))
        return self


class ContextSettings(_Config):
    """Market context for one agent (CHANGE.md P5). Opt-in: ``enabled: false``.

    With it off nothing is fetched, nothing is added to the prompt and the event guard
    has nothing to check. Every source has its own switch; all refreshes are fail-soft
    background jobs off the trade path.
    """

    enabled: bool = False
    refresh_minutes: int = 60
    # Upcoming events within this horizon are shown in the prompt.
    lookahead_hours: float = 48.0
    # Sent with every context HTTP request (SEC EDGAR rejects anonymous clients).
    http_user_agent: str = "trading-agent/0.1 (paper-trading research)"
    http_timeout_seconds: float = 15.0
    sentiment: SentimentSettings = Field(default_factory=SentimentSettings)
    macro: MacroContextSettings = Field(default_factory=MacroContextSettings)
    announcements: AnnouncementsSettings = Field(default_factory=AnnouncementsSettings)
    earnings: EarningsSettings = Field(default_factory=EarningsSettings)
    news: NewsSettings = Field(default_factory=NewsSettings)
    summarizer: SummarizerSettings = Field(default_factory=SummarizerSettings)

    @model_validator(mode="after")
    def _check(self) -> ContextSettings:
        for name in ("refresh_minutes", "lookahead_hours", "http_timeout_seconds"):
            _positive(f"context.{name}", getattr(self, name))
        if self.summarizer.enabled and not self.news.enabled:
            raise ValueError("context.summarizer needs context.news (it digests news items)")
        return self


class MacroCalendarSettings(_Config):
    """Scheduled macro events shared by both agents (option (c)).

    ``events`` is the reliable base — an operator-maintained list (FOMC, ECB …, UTC)
    that works offline. ``feed_url`` adds the ForexFactory weekly JSON on top when
    reachable (unofficial; "" disables it). Each agent picks currencies/importance in
    ``<agent>.context.macro``. Parsed events are dicts with an aware-UTC ``at``.
    """

    feed_url: str | None = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    events: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("feed_url")
    @classmethod
    def _feed(cls, value: str | None) -> str:
        return value or ""

    @field_validator("events")
    @classmethod
    def _parse_events(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for body in events:
            unknown = set(body) - {"at", "title", "currency", "importance"}
            if unknown:
                raise ValueError(f"macro_calendar.events: unknown keys {sorted(unknown)}")
            at = body.get("at")
            if isinstance(at, str):
                try:
                    at = datetime.fromisoformat(at)
                except ValueError as exc:
                    raise ValueError(f"macro_calendar.events: bad 'at' {body.get('at')!r}") from exc
            if not isinstance(at, datetime) or at.tzinfo is None:
                raise ValueError(
                    f"macro_calendar.events: 'at' must be an ISO time with a zone "
                    f"(e.g. 2026-10-28T18:00:00Z), got {body.get('at')!r}"
                )
            title = body.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise ValueError("macro_calendar.events: 'title' must be a string (<= 200 chars)")
            currency = str(body.get("currency", "")).upper()
            if not _CURRENCY_RE.match(currency):
                raise ValueError(f"macro_calendar.events: bad currency {body.get('currency')!r}")
            importance = body.get("importance", "high")
            if importance not in IMPORTANCE_LEVELS:
                raise ValueError(f"macro_calendar.events: bad importance {importance!r}")
            parsed.append(
                {
                    "at": at.astimezone(UTC),
                    "title": title.strip(),
                    "currency": currency,
                    "importance": importance,
                }
            )
        return parsed


# ── Strategy sleeves (§7.71) ──────────────────────────────────


class SleeveSpec(_Config):
    """One strategy sleeve (CHANGE.md §4.1): its own candle ``timeframe``, prompt
    ``playbook``, holding limit (time stop), per-sleeve risk limits and a fixed capital
    ``weight`` — a configuration of the decision pipeline, not a new process."""

    name: str
    timeframe: str
    playbook: str = "swing"
    # Time stop: ``max_hours`` or ``max_days``; omit for none.
    holding: dict[str, float] = Field(default_factory=dict)
    # Fixed share of agent equity (the allocator comes later); None → equal split.
    weight: float | None = None
    # Per-sleeve limits: any RiskSettings field except enforce_exit_levels, layered
    # over the agent-level ``risk:`` block (CHANGE.md §4.8).
    risk: dict[str, Any] = Field(default_factory=dict)

    @property
    def max_holding_hours(self) -> float | None:
        if "max_hours" in self.holding:
            return float(self.holding["max_hours"])
        if "max_days" in self.holding:
            return float(self.holding["max_days"]) * 24.0
        return None

    @property
    def risk_overrides(self) -> dict[str, Any]:
        return self.risk

    @model_validator(mode="after")
    def _check(self) -> SleeveSpec:
        name = self.name
        if not _SLEEVE_NAME_RE.match(name):
            raise ValueError(
                f"sleeve name {name!r} must be lowercase letters/digits/underscores, "
                "starting with a letter, at most 20 characters"
            )
        if timeframe_delta(self.timeframe) is None:
            raise ValueError(
                f"sleeves.{name}.timeframe {self.timeframe!r} is not a candle timeframe"
            )
        if self.playbook not in PLAYBOOK_NAMES:
            raise ValueError(f"sleeves.{name}.playbook must be one of {', '.join(PLAYBOOK_NAMES)}")
        unknown = set(self.holding) - {"max_hours", "max_days"}
        if unknown:
            raise ValueError(f"sleeves.{name}.holding: unknown keys {sorted(unknown)}")
        if len(self.holding) > 1:
            raise ValueError(f"sleeves.{name}.holding: give max_hours OR max_days, not both")
        if self.max_holding_hours is not None and self.max_holding_hours <= 0:
            raise ValueError(f"sleeves.{name}.holding must be > 0")
        if self.weight is not None and not 0.0 < self.weight <= 1.0:
            raise ValueError(f"sleeves.{name}.weight must be in (0, 1]")
        bad = set(self.risk) - (set(RiskSettings.model_fields) - {"enforce_exit_levels"})
        if bad:
            raise ValueError(
                f"sleeves.{name}.risk: unknown or non-overridable fields {sorted(bad)}"
            )
        return self


class SleevesSettings(_Config):
    """Strategy sleeves for one agent. Opt-in: ``enabled: false`` ships.

    Every sleeve decides on its own timeframe with its own playbook; a symbol is held
    by at most one sleeve (symbol lock), evaluated in config order — the first listed
    wins a same-cycle tie. ``strategies`` is a ``name → spec`` mapping in YAML.
    """

    enabled: bool = False
    # Loose agent-wide breaker (CHANGE.md §4.8): agent equity this far below its peak
    # blocks new entries in every sleeve, whatever the sleeves' own limits.
    backstop_max_drawdown_pct: float = 0.20
    strategies: list[SleeveSpec] = Field(default_factory=list)

    @field_validator("strategies", mode="before")
    @classmethod
    def _named(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [{**(body or {}), "name": name} for name, body in value.items()]
        return value

    @model_validator(mode="after")
    def _check(self) -> SleevesSettings:
        if not 0.0 < self.backstop_max_drawdown_pct < 1.0:
            raise ValueError("sleeves.backstop_max_drawdown_pct must be in (0, 1)")
        specs = self.strategies
        if self.enabled and not specs:
            raise ValueError("sleeves.enabled needs at least one entry under sleeves.strategies")
        weights = [s.weight for s in specs]
        if specs and any(w is not None for w in weights):
            if any(w is None for w in weights):
                raise ValueError("sleeves: give every sleeve a weight, or none (equal split)")
            if sum(w for w in weights if w is not None) > 1.0 + 1e-9:
                raise ValueError("sleeves: weights must sum to at most 1.0")
        for spec in specs:
            if spec.weight is None:
                spec.weight = 1.0 / len(specs)
        return self

    def get(self, name: str | None) -> SleeveSpec | None:
        """The sleeve called ``name`` (``None`` when unknown)."""
        return next((s for s in self.strategies if s.name == name), None)


# ── Agents ────────────────────────────────────────────────────


class ExchangeWindow(_Config):
    """One exchange's trading window (§7.66): hours, zone and closures."""

    name: str
    market_hours: str
    market_timezone: str
    market_holidays: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ExchangeWindow:
        name = self.name
        self.market_hours = self.market_hours.strip()
        if not re.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$", self.market_hours):
            raise ValueError(f"exchanges.{name}.market_hours must look like 'HH:MM-HH:MM'")
        try:
            ZoneInfo(self.market_timezone)
        except Exception as exc:
            raise ValueError(
                f"exchanges.{name}.market_timezone {self.market_timezone!r} unknown"
            ) from exc
        for raw in self.market_holidays:
            try:
                date.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError(f"exchanges.{name}.market_holidays: bad date {raw!r}") from exc
        return self


class AgentConfig(_Config):
    enabled: bool
    exchange: str | None = None
    testnet: bool = True
    interval_minutes: int = 5
    pairs: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    # Local wall-clock window + its IANA zone (a UTC host stays correct) and ISO
    # closure dates (§7.10) for the stocks market-hours guard.
    market_hours: str | None = None
    market_timezone: str | None = None
    market_holidays: list[str] = Field(default_factory=list)
    # Prior decisions fed back into the prompt; 0 disables the section.
    decision_history_limit: int = 10
    # Candle timeframe the agent decides on (§7.56); None → crypto "1h", stocks "1d".
    timeframe: str | None = None
    # §7.56: ask the LLM once per newly closed bar; cycles in between only mark
    # positions and enforce exit levels.
    decide_on_new_bar_only: bool = True
    # §7.41: explicit opt-in for a keyed executor on the live venue (real money).
    # Also needs LIVE_TRADING_ACK in the env.
    live_trading: bool = False
    # §7.64: currency the keyed executor counts as cash (e.g. "EUR" on OKX Europe);
    # every pair must be quoted in it.
    quote_currency: str | None = None
    # Prompt playbook for the agent's single implicit style (sleeves set their own);
    # None = the default persona. ``test`` belongs to the test profile only.
    playbook: str | None = None
    # §7.70 screener watchlist, §7.71 strategy sleeves, §7.18 market context.
    watchlist: WatchlistSettings = Field(default_factory=WatchlistSettings)
    sleeves: SleevesSettings = Field(default_factory=SleevesSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    # §7.66 per-exchange windows: symbols in ``symbol_exchanges`` trade in their
    # exchange's window; the rest in the agent-level window above.
    exchanges: dict[str, ExchangeWindow] = Field(default_factory=dict)
    symbol_exchanges: dict[str, str] = Field(default_factory=dict)

    @field_validator("quote_currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @field_validator("exchanges", mode="before")
    @classmethod
    def _named(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                code: {**body, "name": str(code)} if isinstance(body, dict) else body
                for code, body in value.items()
            }
        return value

    @model_validator(mode="after")
    def _check(self) -> AgentConfig:
        if self.playbook is not None and self.playbook not in PLAYBOOK_NAMES:
            raise ValueError(f"playbook must be one of {', '.join(PLAYBOOK_NAMES)}")
        unknown = sorted({v for v in self.symbol_exchanges.values() if v not in self.exchanges})
        if unknown:
            raise ValueError(f"symbol_exchanges names exchanges not configured: {unknown}")
        if self.watchlist.news_mentions.enabled and not (
            self.context.enabled and self.context.news.enabled
        ):
            raise ValueError(
                "watchlist.news_mentions needs context.enabled and context.news.enabled "
                "(mentions are counted over ingested news)"
            )
        mismatched = pairs_not_quoted_in(self.pairs, self.quote_currency)
        if mismatched:
            raise ValueError(
                f"pairs {mismatched} are not quoted in quote_currency "
                f"'{self.quote_currency}' — cash and position sizing would be wrong"
            )
        return self


# ── Execution, storage, monitoring ────────────────────────────


class ExecutionSettings(_Config):
    """Parameters for the simulated (paper) executor.

    Config-driven so paper PnL — which the LLM is shown and the live-readiness gates
    compare against — reflects real trading costs. **Per-venue cost profiles (§7.65):**
    the flat ``paper_*`` fields are the default schedule; ``paper_costs`` overrides them
    per runner component (``crypto`` / ``stocks``) so each paper book simulates its
    venue — OKX EU spot (taker 0.20 %, §7.75) vs Saxo US stocks (0.08 % with a min $1
    plus 0.25 % FX). :meth:`paper_cost_params` resolves the effective schedule.
    """

    #: Overridable per-venue cost fields.
    COST_FIELDS: ClassVar[tuple[str, ...]] = (
        "paper_fee_pct",
        "paper_slippage_pct",
        "paper_min_commission",
        "paper_fx_fee_pct",
    )

    paper_fee_pct: float = 0.0
    paper_slippage_pct: float = 0.001  # per-side slippage (0.1 %)
    # Starting bankroll of a *fresh* paper portfolio; the persisted snapshot wins after.
    initial_cash: float = 100_000.0
    # Absolute commission floor per side, in the book's currency. 0 = percentage-only.
    paper_min_commission: float = 0.0
    # Per side when the profile models trades settling in a foreign currency.
    paper_fx_fee_pct: float = 0.0
    # Per-component overrides, e.g. {"stocks": {"paper_min_commission": 1.0}}.
    paper_costs: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("paper_costs", mode="before")
    @classmethod
    def _check_costs(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        for agent, overrides in value.items():
            if not isinstance(agent, str) or not isinstance(overrides, dict):
                raise ValueError(  # noqa: TRY004 - a YAML config error, not a type error
                    "execution.paper_costs must map component names to dicts of cost fields"
                )
            for field, number in overrides.items():
                if field not in cls.COST_FIELDS:
                    raise ValueError(
                        f"execution.paper_costs.{agent}: unknown field {field!r} "
                        f"(allowed: {', '.join(cls.COST_FIELDS)})"
                    )
                if not isinstance(number, (int, float)) or isinstance(number, bool) or number < 0:
                    raise ValueError(
                        f"execution.paper_costs.{agent}.{field} must be a non-negative number"
                    )
        return value

    def paper_cost_params(self, agent: str | None = None) -> dict[str, float]:
        """Effective cost params for ``agent``: flat defaults merged with its profile."""
        params = {field: getattr(self, field) for field in self.COST_FIELDS}
        params.update(self.paper_costs.get(agent or "", {}))
        return params


class StorageSettings(_Config):
    """Where the per-agent × mode SQLite files live, and how they are kept (§7.78).

    ``data_dir`` holds ``<mode>_<agent>.db`` files (:mod:`src.core.db_layout`) — the
    runner derives the file from the executor's venue, never from config.
    ``in_memory`` keeps one in-memory database instead (tests).
    """

    data_dir: str = "data"
    in_memory: bool = False
    # Retention (§7.12): market snapshots are re-creatable cache and prune by default;
    # decisions/orders are the trade record, kept unless bounded (0 = forever);
    # portfolio snapshots are never pruned (the drawdown seed reads their history).
    snapshot_retention_days: int = 30
    history_retention_days: int = 0
    prune_interval_minutes: int = 1440
    # Online DB backup before every prune pass (§7.35); "" disables. backup_keep
    # rotates old ones (0 keeps all).
    backup_dir: str = ""
    backup_keep: int = 0
    # Market-context rows (§7.18) older than this are pruned; 0 keeps them.
    context_retention_days: int = 30

    @model_validator(mode="after")
    def _check(self) -> StorageSettings:
        if self.context_retention_days < 0:
            raise ValueError("storage.context_retention_days must be >= 0")
        return self


class MonitoringSettings(_Config):
    log_level: str = "INFO"
    alert_dedup_window_seconds: int = 300
    # §7.51 webhook channel. The URL is a secret: ALERT_WEBHOOK_URL env only.
    alert_webhook_format: str = "json"
    alert_min_severity: str = "warning"

    @model_validator(mode="after")
    def _check(self) -> MonitoringSettings:
        if self.alert_webhook_format not in ("json", "ntfy"):
            raise ValueError("monitoring.alert_webhook_format must be 'json' or 'ntfy'")
        if self.alert_min_severity not in ("info", "warning", "error"):
            raise ValueError("monitoring.alert_min_severity must be info, warning or error")
        return self


class ControlApiSettings(_Config):
    """Agent-side control API (§7.15 P2): off unless enabled; loopback by default.

    Exposes only safe config + control latches; credentials are structurally absent.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    crypto_port: int = 8101
    stocks_port: int = 8102
    # Extra Host names accepted besides loopback + ``host`` (§7.43 DNS rebinding).
    allowed_hosts: list[str] = Field(default_factory=list)


class DashboardSettings(_Config):
    """Standalone web dashboard (§7.15 P3–P4): FastAPI + Jinja2/HTMX.

    Opens every per-mode book (§7.78) as a WAL reader and writes the ``agent_control``
    latches directly. Loopback by default; only the safe config surface is editable.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    # HTMX polling interval for the live fragments (at least 1 s).
    refresh_seconds: int = 5
    # Which control rows to show/control (empty → both built-in agents).
    agents: list[str] = Field(default_factory=lambda: ["crypto", "stocks"])
    # §7.24 opt-in Start/Stop buttons for local runners; keep off under compose.
    allow_launch: bool = False
    # Extra Host names besides loopback + ``host`` (§7.43 DNS rebinding).
    allowed_hosts: list[str] = Field(default_factory=list)
    # IANA zone the pages show times in (the DB stores UTC); None = the host's local
    # zone — which is UTC inside a container, hence an explicit zone in settings.yaml.
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(
                    f"dashboard.timezone '{value}' is not an IANA zone (e.g. Europe/Bratislava)"
                ) from None
        return value

    @model_validator(mode="after")
    def _check(self) -> DashboardSettings:
        self.refresh_seconds = max(1, self.refresh_seconds)
        self.agents = self.agents or ["crypto", "stocks"]
        return self


# ── Venues ────────────────────────────────────────────────────


def _check_symbol_map(block: str, value: Any) -> Any:
    """Data → venue symbol table: strings to non-empty strings, one-to-one."""
    if not isinstance(value, dict):
        return value
    for key, target in value.items():
        if not isinstance(key, str) or not isinstance(target, str) or not target:
            raise ValueError(f"{block}.symbol_map must map symbol strings to strings")
    if len(set(value.values())) != len(value):
        raise ValueError(f"{block}.symbol_map must be one-to-one")
    return value


class XTBExecutionSettings(_Config):
    """XTB demo execution via xAPI (§7.16): off unless enabled — DEAD PATH (§7.66).

    Deliberately outside the dashboard's safe-config whitelist: enabling real
    execution must never be a web-form click.
    """

    enabled: bool = False
    host: str = "wss://ws.xapi.pro"
    account_type: str = "demo"
    request_timeout_seconds: float = 10.0
    # Data → xAPI symbols (§7.59 L8), e.g. {"AAPL": "AAPL.US"}; unmapped pass through.
    symbol_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("symbol_map", mode="before")
    @classmethod
    def _map(cls, value: Any) -> Any:
        return _check_symbol_map("xtb_execution", value)

    @model_validator(mode="after")
    def _check(self) -> XTBExecutionSettings:
        if self.account_type not in ("demo", "real"):
            raise ValueError("xtb_execution.account_type must be 'demo' or 'real'")
        return self


class VenueOrderSettings(_Config):
    """How a keyed ccxt venue executor prices and ages its orders (§7.75).

    The pipeline hands every order a *reference* price (the last close); the executor
    turns it into a venue order: BUY = limit at ``close × (1 + entry_offset_pct)``
    (crosses the spread; sizing reserves the offset); SELL = ``market`` by default (an
    exit must fill) or a limit at ``close × (1 − exit_offset_pct)``; an order still
    working after ``order_ttl_seconds`` is cancelled (0 = never); one ``fetch_order``
    ``fill_confirm_delay_seconds`` after placing resolves the fill in the same cycle.
    Paper execution is unaffected. Venue plumbing, not a risk knob.
    ``protective_orders`` (§7.34) mirrors each position's SL/TP as a venue OCO.
    """

    EXIT_ORDER_TYPES: ClassVar[tuple[str, ...]] = ("market", "limit")

    entry_offset_pct: float = 0.002
    exit_order_type: str = "market"
    exit_offset_pct: float = 0.005
    order_ttl_seconds: float = 600.0
    fill_confirm_delay_seconds: float = 1.0
    # §7.34: keep an OCO (stop-loss + take-profit) at the venue for every open
    # position, so it is protected while the agent is down. Local checks stay.
    protective_orders: bool = False

    @model_validator(mode="after")
    def _check(self) -> VenueOrderSettings:
        if self.exit_order_type not in self.EXIT_ORDER_TYPES:
            raise ValueError("venue_orders.exit_order_type must be 'market' or 'limit'")
        for name in ("entry_offset_pct", "exit_offset_pct"):
            if not 0 <= getattr(self, name) < 0.05:
                raise ValueError(f"venue_orders.{name} must be in [0, 0.05)")
        for name in ("order_ttl_seconds", "fill_confirm_delay_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"venue_orders.{name} must be non-negative seconds")
        return self


class SaxoOAuthSettings(_Config):
    """Saxo OAuth app (§7.66 step 4). App key/secret come from the environment
    (``SAXO_APP_KEY`` / ``SAXO_APP_SECRET``); the token pair lives in ``token_file``."""

    enabled: bool = False
    # Must equal the app's registered redirect URL; scripts/saxo_login.py listens here.
    redirect_uri: str = "http://localhost:8765/callback"
    # None → sim.logonvalidation.net / live.logonvalidation.net by environment.
    auth_base_url: str | None = None
    # None → <storage.data_dir>/saxo_<environment>.token.json (0600, gitignored).
    token_file: str | None = None
    refresh_margin_seconds: float = 120.0
    # Rotate the pair this often while running — SIM refresh tokens live 40 min and
    # the stocks agent makes no API calls outside market hours.
    keepalive_minutes: float = 10.0

    @model_validator(mode="after")
    def _check(self) -> SaxoOAuthSettings:
        if not self.redirect_uri.startswith(("http://", "https://")):
            raise ValueError("saxo_execution.oauth.redirect_uri must be an http(s) URL")
        if self.refresh_margin_seconds < 0 or self.keepalive_minutes < 0:
            raise ValueError("saxo_execution.oauth margins/intervals must be >= 0")
        self.auth_base_url = self.auth_base_url or None
        self.token_file = self.token_file or None
        return self

    def token_path(self, data_dir: str, environment: str) -> Path:
        return Path(self.token_file or Path(data_dir) / f"saxo_{environment}.token.json")


class SaxoExecutionSettings(_Config):
    """Stocks execution on Saxo OpenAPI (§7.66): off unless enabled.

    ``environment: sim`` (Saxo's free simulation account) by default; ``live`` also
    needs ``LIVE_TRADING_ACK`` (§7.41). Outside the dashboard's safe-config whitelist.
    """

    enabled: bool = False
    environment: str = "sim"
    # Which account trades: an explicit AccountKey, else the only active account in
    # ``account_currency`` (US stocks need a USD account — one currency).
    account_key: str | None = None
    account_currency: str | None = "USD"
    # Data → Saxo symbol, e.g. {"AAPL": "AAPL:xnas"}; unmapped symbols must resolve
    # unambiguously.
    symbol_map: dict[str, str] = Field(default_factory=dict)
    request_timeout_seconds: float = 10.0
    # Waits (s) between fill polls right after placing; still working after the last
    # one → resolved by per-cycle reconciliation.
    fill_poll_delays: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
    # Share-quantity precision: 0 = whole shares.
    amount_decimals: int = 0
    oauth: SaxoOAuthSettings = Field(default_factory=SaxoOAuthSettings)

    @field_validator("symbol_map", mode="before")
    @classmethod
    def _map(cls, value: Any) -> Any:
        return _check_symbol_map("saxo_execution", value)

    @model_validator(mode="after")
    def _check(self) -> SaxoExecutionSettings:
        if self.environment not in ("sim", "live"):
            raise ValueError("saxo_execution.environment must be 'sim' or 'live'")
        if not self.fill_poll_delays or any(d < 0 for d in self.fill_poll_delays):
            raise ValueError("saxo_execution.fill_poll_delays must be non-negative seconds")
        if not 0 <= self.amount_decimals <= 8:
            raise ValueError("saxo_execution.amount_decimals must be between 0 and 8")
        self.account_key = self.account_key or None
        self.account_currency = self.account_currency.upper() if self.account_currency else None
        return self


# ── The whole file ────────────────────────────────────────────


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """``overlay`` merged into a copy of ``base``: nested mappings merge, the rest replaces."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Settings:
    """Application settings loaded from config/settings.yaml and .env.

    ``profile`` overlays ``config/profiles/<profile>.yaml`` (next to the settings file)
    on top — e.g. ``test``: permissive risk + a trading playbook for exercising the
    order path. The runner refuses any profile on a real-money account.
    """

    def __init__(self, config_path: str | None = None, profile: str | None = None) -> None:
        path = (
            Path(config_path)
            if config_path
            else Path(__file__).parents[2] / "config" / "settings.yaml"
        )
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f)
        self.profile = profile
        if profile is not None:
            if not re.match(r"^[a-z][a-z0-9_]{0,11}$", profile):
                raise ValueError(
                    f"profile name {profile!r}: lowercase letters/digits/_, ≤ 12 chars"
                )
            profile_path = path.parent / "profiles" / f"{profile}.yaml"
            if not profile_path.exists():
                raise ValueError(f"unknown settings profile {profile!r} (no {profile_path})")
            with open(profile_path) as f:
                raw = deep_merge(raw, yaml.safe_load(f) or {})

        self.llm = LLMSettings(**raw["llm"])
        # YAML copy of the LLM block: dashboard LLM overrides (§7.91) resolve as
        # baseline + override, like risk/execution below.
        self.llm_baseline = LLMSettings(**raw["llm"])
        self.crypto_agent = AgentConfig(**raw["crypto_agent"])
        self.stocks_agent = AgentConfig(**raw["stocks_agent"])
        self.risk = RiskSettings(**raw["risk"])
        # Untouched YAML copies (§7.43/§7.50): safe-config overrides are applied as
        # *baseline + override* every time (removing one reverts the live object) and
        # risk overrides may only tighten relative to these.
        self.risk_baseline = RiskSettings(**raw["risk"])
        self.agent_baselines: dict[str, AgentConfig] = {
            "crypto": AgentConfig(**raw["crypto_agent"]),
            "stocks": AgentConfig(**raw["stocks_agent"]),
        }
        self.execution = ExecutionSettings(**raw.get("execution", {}))
        self.execution_baseline = ExecutionSettings(**raw.get("execution", {}))
        self.storage = StorageSettings(**raw["storage"])
        self.monitoring = MonitoringSettings(**raw["monitoring"])
        self.control_api = ControlApiSettings(**raw.get("control_api", {}))
        self.dashboard = DashboardSettings(**raw.get("dashboard", {}))
        self.xtb_execution = XTBExecutionSettings(**raw.get("xtb_execution", {}))
        self.saxo_execution = SaxoExecutionSettings(**raw.get("saxo_execution", {}))
        self.venue_orders = VenueOrderSettings(**raw.get("venue_orders", {}))
        self.macro_calendar = MacroCalendarSettings(**(raw.get("macro_calendar") or {}))
        if self.xtb_execution.enabled and self.saxo_execution.enabled:
            raise ValueError("enable at most one stocks venue: xtb_execution or saxo_execution")
