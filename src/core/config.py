"""Configuration loading from YAML + environment variables."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..analysis.candles import timeframe_delta

#: Environment acknowledgement required, together with an explicit config flag,
#: before any executor may touch a REAL-money account (§7.41): a keyed exchange run
#: with ``testnet: false`` and ``xtb_execution.account_type: real``.
LIVE_TRADING_ACK_ENV = "LIVE_TRADING_ACK"

#: Env override for the LLM endpoint — any OpenAI-compatible server (LM Studio,
#: Unsloth desktop, llama-server, Ollama). ``LM_STUDIO_ENDPOINT`` is its deprecated
#: former name, still honored (with a warning) so an old ``.env`` keeps working.
LLM_ENDPOINT_ENV = "LOCAL_LLM_ENDPOINT"
LEGACY_LLM_ENDPOINT_ENV = "LM_STUDIO_ENDPOINT"
#: Env opt-in for strict JSON ``response_format``; ``LM_STUDIO_USE_JSON_SCHEMA`` is its
#: deprecated former name (still honored, with a warning) — same rename as the endpoint.
LLM_JSON_SCHEMA_ENV = "LOCAL_LLM_USE_JSON_SCHEMA"
LEGACY_LLM_JSON_SCHEMA_ENV = "LM_STUDIO_USE_JSON_SCHEMA"
LIVE_TRADING_ACK_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"


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


class Settings:
    """Application settings loaded from config/settings.yaml and .env."""

    def __init__(self, config_path: str | None = None) -> None:
        path = (
            Path(config_path)
            if config_path
            else Path(__file__).parents[2] / "config" / "settings.yaml"
        )
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f)

        self.llm = LLMSettings(**raw["llm"])
        self.crypto_agent = AgentConfig(**raw["crypto_agent"])
        self.stocks_agent = AgentConfig(**raw["stocks_agent"])
        self.risk = RiskSettings(**raw["risk"])
        # Untouched copy of the YAML risk limits (§7.43): safe-config overrides may
        # only *tighten* relative to these. ``self.risk`` itself is mutated in place
        # by applied overrides, so it cannot serve as the baseline.
        self.risk_baseline = RiskSettings(**raw["risk"])
        # Untouched YAML copies for the same reason (§7.50): safe-config overrides are
        # applied as *baseline + override* every time, so removing an override reverts
        # the live object instead of leaving a stale value pinned forever.
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


class LLMSettings:
    def __init__(
        self,
        endpoint: str,
        model: str,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        use_json_schema: bool = False,
        # Sampling knobs were hardcoded in the client ("config-driven" rule, §7.19).
        temperature: float = 0.2,
        max_tokens: int = 1024,
        # Base delay for the exponential backoff between retry attempts; 0 disables
        # sleeping (used by tests).
        retry_backoff_base_seconds: float = 1.0,
        # Deterministic-evaluation seed (§7.33): sent with every request when set
        # (models that support it reproduce outputs; LM Studio honors `seed`).
        # None omits the field entirely — provider default behavior.
        seed: int | None = None,
        # Upper bound on a raw completion's character count before parsing (§7.33):
        # a runaway/degenerate generation is treated as a failed attempt, never
        # fed into the signal parser.
        max_response_chars: int = 20_000,
    ) -> None:
        env_endpoint = os.getenv(LLM_ENDPOINT_ENV, "").strip()
        legacy_endpoint = os.getenv(LEGACY_LLM_ENDPOINT_ENV, "").strip()
        if not env_endpoint and legacy_endpoint:
            import structlog

            structlog.get_logger().warning(
                f"{LEGACY_LLM_ENDPOINT_ENV} is deprecated — rename it to {LLM_ENDPOINT_ENV}"
            )
            env_endpoint = legacy_endpoint
        self.endpoint = env_endpoint or endpoint
        # Bearer key for servers that require one (Unsloth desktop, llama-server
        # --api-key, vLLM). A secret: env ``LLM_API_KEY`` only — never YAML, never
        # logged. Unset (LM Studio's default) sends no Authorization header.
        self.api_key = os.getenv("LLM_API_KEY", "").strip() or None
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.seed = seed
        self.max_response_chars = max_response_chars
        # Opt-in: request a strict JSON response schema. Enable once you've
        # confirmed the local model supports ``response_format`` — some setups
        # reject it, which would otherwise force the safe HOLD fallback every cycle.
        env_schema = os.getenv(LLM_JSON_SCHEMA_ENV, "").strip()
        legacy_schema = os.getenv(LEGACY_LLM_JSON_SCHEMA_ENV, "").strip()
        if not env_schema and legacy_schema:
            import structlog

            structlog.get_logger().warning(
                f"{LEGACY_LLM_JSON_SCHEMA_ENV} is deprecated — rename it to {LLM_JSON_SCHEMA_ENV}"
            )
            env_schema = legacy_schema
        self.use_json_schema = use_json_schema or env_schema.lower() in ("1", "true", "yes")


#: Removed agent keys still tolerated in old configs (warned, ignored).
_LEGACY_AGENT_KEYS: frozenset[str] = frozenset({"broker", "demo"})


class AgentConfig:
    def __init__(
        self,
        enabled: bool,
        exchange: str | None = None,
        testnet: bool = True,
        interval_minutes: int = 5,
        pairs: list[str] | None = None,
        symbols: list[str] | None = None,
        market_hours: str | None = None,
        market_timezone: str | None = None,
        # Exchange closure dates (§7.10): ISO strings ("YYYY-MM-DD"). Weekends are
        # always closed; this list covers holidays and one-off shutdowns.
        market_holidays: list[str] | None = None,
        decision_history_limit: int = 10,
        # Candle timeframe the agent decides on (§7.56) — was hardcoded per agent.
        # None → the agent's default (crypto "1h", stocks "1d").
        timeframe: str | None = None,
        # §7.56: ask the LLM once per newly closed bar; cycles in between only mark
        # positions and enforce exit levels.
        decide_on_new_bar_only: bool = True,
        # §7.41: explicit opt-in for a keyed executor on the live venue (real money).
        # Also needs LIVE_TRADING_ACK in the env.
        live_trading: bool = False,
        # §7.64: currency the keyed executor counts as cash (e.g. "EUR" on OKX Europe,
        # where USDT is not tradable for EEA accounts). Every pair must be quoted in it.
        quote_currency: str | None = None,
        # §7.70: screener-driven dynamic watchlist (CHANGE.md §4.4, crypto first).
        # Off by default — with no block the traded set stays exactly the YAML list.
        watchlist: dict[str, Any] | None = None,
        # §7.71: strategy sleeves (CHANGE.md P1) — several trading styles side by side
        # in this agent's process. Off by default: one implicit style, as before.
        sleeves: dict[str, Any] | None = None,
        # §7.18: market context (sentiment, calendars, venue notices, news + LLM
        # context cards) for the prompt and the event guard. Off by default.
        context: dict[str, Any] | None = None,
        # §7.66: per-exchange trading windows for a mixed US + EU stocks universe.
        # ``symbol_exchanges`` maps a symbol to one of ``exchanges``; unmapped symbols
        # use the agent-level market_hours/market_timezone/market_holidays above.
        exchanges: dict[str, dict[str, Any]] | None = None,
        symbol_exchanges: dict[str, str] | None = None,
        **legacy: Any,
    ) -> None:
        # ``broker``/``demo`` were XTB-era stocks keys nothing ever read (the venue is
        # chosen by saxo_execution / xtb_execution). Tolerated with a warning so an old
        # settings.yaml still loads; any other unknown key is still a config error.
        unknown = set(legacy) - _LEGACY_AGENT_KEYS
        if unknown:
            raise TypeError(f"unexpected agent config keys: {sorted(unknown)}")
        if legacy:
            import structlog

            structlog.get_logger().warning(
                "ignoring obsolete agent config keys — remove them from settings.yaml",
                keys=sorted(legacy),
            )
        self.enabled = enabled
        self.exchange = exchange
        self.testnet = testnet
        self.interval_minutes = interval_minutes
        self.pairs = pairs or []
        self.symbols = symbols or []
        self.market_hours = market_hours
        # IANA zone for the market-hours guard (e.g. "America/New_York"). The window
        # is a local wall-clock range, so ``now`` is rendered in this zone before
        # comparison — a UTC host otherwise runs the guard 1–2h off. Falls back
        # to the agent's default zone when unset.
        self.market_timezone = market_timezone
        self.market_holidays = market_holidays or []
        # How many of this agent's prior decisions to feed back into the prompt
        # prompt ("learn from its own track record"). 0 disables the section.
        self.decision_history_limit = decision_history_limit
        self.timeframe = timeframe
        self.decide_on_new_bar_only = decide_on_new_bar_only
        self.live_trading = bool(live_trading)
        self.quote_currency = quote_currency.upper() if quote_currency else None
        self.watchlist = WatchlistSettings(**(watchlist or {}))
        self.sleeves = SleevesSettings(**(sleeves or {}))
        self.context = ContextSettings(**(context or {}))
        self.exchanges: dict[str, ExchangeWindow] = {}
        for code, body in (exchanges or {}).items():
            if not isinstance(body, dict):
                raise ValueError(f"exchanges.{code} must be a mapping")  # noqa: TRY004
            self.exchanges[str(code)] = ExchangeWindow(name=str(code), **body)
        self.symbol_exchanges: dict[str, str] = dict(symbol_exchanges or {})
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
        if self.quote_currency:
            mismatched = pairs_not_quoted_in(self.pairs, self.quote_currency)
            if mismatched:
                raise ValueError(
                    f"pairs {mismatched} are not quoted in quote_currency "
                    f"'{self.quote_currency}' — cash and position sizing would be wrong"
                )


class ExchangeWindow:
    """One exchange's trading window (§7.66): hours, zone and closures, validated eagerly."""

    def __init__(
        self,
        name: str,
        market_hours: str,
        market_timezone: str,
        market_holidays: list[str] | None = None,
    ) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        if not re.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$", str(market_hours).strip()):
            raise ValueError(f"exchanges.{name}.market_hours must look like 'HH:MM-HH:MM'")
        try:
            ZoneInfo(market_timezone)
        except Exception as exc:
            raise ValueError(
                f"exchanges.{name}.market_timezone {market_timezone!r} unknown"
            ) from exc
        for raw in market_holidays or []:
            try:
                date.fromisoformat(str(raw))
            except ValueError as exc:
                raise ValueError(f"exchanges.{name}.market_holidays: bad date {raw!r}") from exc
        self.name = name
        self.market_hours = str(market_hours).strip()
        self.market_timezone = market_timezone
        self.market_holidays = [str(d) for d in (market_holidays or [])]


class WatchlistSettings:
    """Deterministic screener + capped watchlist manager (§7.70, CHANGE.md §4.4).

    All limits are hard-coded guards applied by code, never model judgement. The
    whole feature is opt-in (``enabled: false`` default): with it off the agent
    trades exactly its YAML symbol list, as before.
    """

    def __init__(
        self,
        enabled: bool = False,
        # How often the runner re-runs the screener while alive.
        refresh_minutes: int = 360,
        # Hard cap on manager-added symbols (core YAML symbols never count).
        max_dynamic_symbols: int = 2,
        # A dynamic symbol is dropped (and its slot freed) after this many hours
        # unless the screener re-adds it. Symbols with an open position are always
        # kept regardless of expiry — a position must never lose its manager.
        ttl_hours: float = 96.0,
        # Liquidity floor: 24 h traded volume in quote currency (e.g. EUR).
        min_quote_volume_24h: float = 1_000_000.0,
        # Momentum ranking window / candle lookback (daily bars).
        momentum_days: int = 14,
        lookback_days: int = 30,
        # Volatility band on daily close-to-close returns: below the floor the
        # instrument is dead-flat, above the cap it is a blow-up risk.
        min_daily_volatility: float = 0.005,
        max_daily_volatility: float | None = 0.25,
        # Candle fetches per refresh are capped to this many best-liquidity
        # candidates (after the volume floor) so a huge venue never spams the API.
        max_candidates: int = 20,
        # Symbols the manager must never add (stables, wrapped/leveraged tokens…).
        exclude_symbols: list[str] | None = None,
        # §7.83: candidates named in recent news move ahead of unmentioned ones
        # (after every filter). Needs <agent>.context.news.
        news_mentions: dict[str, Any] | None = None,
    ) -> None:
        if refresh_minutes < 1:
            raise ValueError("watchlist.refresh_minutes must be >= 1")
        if max_dynamic_symbols < 1:
            raise ValueError("watchlist.max_dynamic_symbols must be >= 1")
        if ttl_hours <= 0:
            raise ValueError("watchlist.ttl_hours must be > 0")
        if momentum_days < 1:
            raise ValueError("watchlist.momentum_days must be >= 1")
        if lookback_days < momentum_days + 2:
            raise ValueError(
                "watchlist.lookback_days must cover the momentum window plus two extra "
                "closes (lookback_days >= momentum_days + 2), otherwise metrics never compute"
            )
        if min_daily_volatility < 0:
            raise ValueError("watchlist.min_daily_volatility must be >= 0")
        if max_daily_volatility is not None and max_daily_volatility < min_daily_volatility:
            raise ValueError(
                "watchlist.max_daily_volatility must be >= watchlist.min_daily_volatility"
            )
        if max_candidates < 1:
            raise ValueError("watchlist.max_candidates must be >= 1")
        self.enabled = bool(enabled)
        self.refresh_minutes = int(refresh_minutes)
        self.max_dynamic_symbols = int(max_dynamic_symbols)
        self.ttl_hours = float(ttl_hours)
        self.min_quote_volume_24h = float(min_quote_volume_24h)
        self.momentum_days = int(momentum_days)
        self.lookback_days = int(lookback_days)
        self.min_daily_volatility = float(min_daily_volatility)
        self.max_daily_volatility = (
            float(max_daily_volatility) if max_daily_volatility is not None else None
        )
        self.max_candidates = int(max_candidates)
        self.exclude_symbols = [s.upper() for s in (exclude_symbols or [])]
        self.news_mentions = NewsMentionSettings(**(news_mentions or {}))


class NewsMentionSettings:
    """News mentions as a watchlist priority (§7.83, CHANGE.md §4.4). Off by default."""

    def __init__(
        self, enabled: bool = False, lookback_hours: float = 48.0, min_mentions: int = 2
    ) -> None:
        if float(lookback_hours) <= 0:
            raise ValueError("watchlist.news_mentions.lookback_hours must be > 0")
        if int(min_mentions) < 1:
            raise ValueError("watchlist.news_mentions.min_mentions must be >= 1")
        self.enabled = bool(enabled)
        self.lookback_hours = float(lookback_hours)
        self.min_mentions = int(min_mentions)


#: Event importance levels, lowest first (§7.18; mirrors ``models.EventImportance``).
IMPORTANCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")

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
        "max_response_chars",
    }
)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _positive(name: str, value: float) -> float:
    if float(value) <= 0:
        raise ValueError(f"{name} must be > 0")
    return float(value)


class SentimentSettings:
    """Market-wide sentiment feed (§7.18). Shipped source: crypto Fear & Greed."""

    SOURCES: tuple[str, ...] = ("fear_greed",)

    def __init__(
        self,
        enabled: bool = False,
        source: str = "fear_greed",
        url: str = "https://api.alternative.me/fng/?limit=2",
        # Older readings are not shown (the index updates daily).
        max_age_hours: float = 36.0,
    ) -> None:
        if source not in self.SOURCES:
            raise ValueError(f"context.sentiment.source must be one of {', '.join(self.SOURCES)}")
        self.enabled = bool(enabled)
        self.source = source
        self.url = url
        self.max_age_hours = _positive("context.sentiment.max_age_hours", max_age_hours)


class MacroContextSettings:
    """Which macro events (``macro_calendar``) this agent cares about (§7.18)."""

    def __init__(
        self,
        enabled: bool = False,
        # Economies whose releases move this market (USD CPI/FOMC move crypto too).
        currencies: list[str] | None = None,
        # Events below this importance are not stored (nor shown, nor guarded).
        min_importance: str = "high",
    ) -> None:
        currencies = [c.upper() for c in (currencies or ["USD", "EUR"])]
        bad = [c for c in currencies if not _CURRENCY_RE.match(c)]
        if bad:
            raise ValueError(f"context.macro.currencies: not ISO currency codes: {bad}")
        if min_importance not in IMPORTANCE_LEVELS:
            raise ValueError(
                f"context.macro.min_importance must be one of {', '.join(IMPORTANCE_LEVELS)}"
            )
        self.enabled = bool(enabled)
        self.currencies = currencies
        self.min_importance = min_importance


class AnnouncementsSettings:
    """Venue announcements (§7.18): OKX delisting notices, public API, no key."""

    def __init__(
        self,
        enabled: bool = False,
        # OKX Europe's host (§7.64); the global site serves the same endpoint.
        base_url: str = "https://eea.okx.com",
        # How far back to keep matching a notice to a traded asset for the prompt.
        max_age_days: int = 120,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = base_url.rstrip("/")
        self.max_age_days = int(_positive("context.announcements.max_age_days", max_age_days))


class EarningsSettings:
    """Stock earnings dates via yfinance (§7.18; stocks agent)."""

    def __init__(self, enabled: bool = False, lookahead_days: int = 45) -> None:
        self.enabled = bool(enabled)
        self.lookahead_days = int(_positive("context.earnings.lookahead_days", lookahead_days))


class NewsFeedSpec:
    """One RSS/Atom feed. ``symbols`` pins every item to those symbols (e.g. an
    EDGAR per-company filings feed); otherwise items match by keyword aliases."""

    def __init__(self, name: str, url: str, symbols: list[str] | None = None) -> None:
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise ValueError("context.news.feeds[].name must be a non-empty string (<= 40 chars)")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ValueError(f"context.news.feeds.{name}.url must be an http(s) URL")
        self.name = name.strip()
        self.url = url
        self.symbols = list(symbols or [])


class NewsSettings:
    """RSS/Atom news + filings ingest (§7.18). Raw text feeds only the summarizer."""

    def __init__(
        self,
        enabled: bool = False,
        feeds: list[dict[str, Any]] | None = None,
        # Extra keywords per symbol (case-insensitive, whole word). The base asset /
        # ticker itself always matches, e.g. {"BTC/EUR": ["bitcoin"]}.
        aliases: dict[str, list[str]] | None = None,
        max_items_per_feed: int = 30,
        max_item_chars: int = 2000,
        # Older items are ignored at ingest.
        max_age_hours: float = 48.0,
        # Response size cap per feed download.
        max_feed_bytes: int = 2_000_000,
    ) -> None:
        specs: list[NewsFeedSpec] = []
        for body in feeds or []:
            if not isinstance(body, dict):
                raise ValueError("context.news.feeds entries must be mappings")  # noqa: TRY004
            specs.append(NewsFeedSpec(**body))
        if enabled and not specs:
            raise ValueError("context.news.enabled needs at least one entry under feeds")
        clean_aliases: dict[str, list[str]] = {}
        for symbol, words in (aliases or {}).items():
            if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
                raise ValueError(f"context.news.aliases.{symbol} must be a list of strings")
            clean_aliases[symbol] = [w.lower() for w in words]
        self.enabled = bool(enabled)
        self.feeds = specs
        self.aliases = clean_aliases
        self.max_items_per_feed = int(
            _positive("context.news.max_items_per_feed", max_items_per_feed)
        )
        self.max_item_chars = int(_positive("context.news.max_item_chars", max_item_chars))
        self.max_age_hours = _positive("context.news.max_age_hours", max_age_hours)
        self.max_feed_bytes = int(_positive("context.news.max_feed_bytes", max_feed_bytes))


class SummarizerSettings:
    """Batch LLM summarizer → per-symbol context cards (§7.18, CHANGE.md §4.4).

    Runs off the trade path on its own cadence and shares one in-process lock with
    the trading LLM client, so the local server never serves both at once. ``llm``
    overrides fields of the top-level ``llm:`` block (e.g. a smaller ``model`` —
    CHANGE.md Q7); unset → the trading model.
    """

    def __init__(
        self,
        enabled: bool = False,
        refresh_minutes: int = 240,
        card_ttl_hours: float = 12.0,
        # News window a card digests, and how many items it may read.
        lookback_hours: float = 24.0,
        max_items_per_card: int = 8,
        # At most this many symbols are summarized per pass (LLM budget, CHANGE.md §4.7).
        max_symbols_per_run: int = 5,
        llm: dict[str, Any] | None = None,
    ) -> None:
        llm = dict(llm or {})
        bad = set(llm) - _SUMMARIZER_LLM_FIELDS
        if bad:
            raise ValueError(f"context.summarizer.llm: unknown or forbidden fields {sorted(bad)}")
        self.enabled = bool(enabled)
        self.refresh_minutes = int(_positive("context.summarizer.refresh_minutes", refresh_minutes))
        self.card_ttl_hours = _positive("context.summarizer.card_ttl_hours", card_ttl_hours)
        self.lookback_hours = _positive("context.summarizer.lookback_hours", lookback_hours)
        self.max_items_per_card = int(
            _positive("context.summarizer.max_items_per_card", max_items_per_card)
        )
        self.max_symbols_per_run = int(
            _positive("context.summarizer.max_symbols_per_run", max_symbols_per_run)
        )
        self.llm_overrides: dict[str, Any] = llm


def summarizer_llm_settings(base: LLMSettings, overrides: dict[str, Any]) -> LLMSettings:
    """The summarizer's LLM settings: the trading block with ``overrides`` applied (§7.18).

    A shallow copy, so the endpoint/API key resolved from the environment carry over
    and nothing written here leaks back into the trading client.
    """
    import copy

    derived = copy.copy(base)
    for name, value in overrides.items():
        setattr(derived, name, value)
    return derived


class ContextSettings:
    """Market context for one agent (§7.18, CHANGE.md P5). Opt-in: ``enabled: false``.

    With it off nothing is fetched, nothing is added to the prompt and the event
    guard has nothing to check — behavior is exactly as before. Every source has its
    own switch; all refreshes are fail-soft background jobs off the trade path.
    """

    def __init__(
        self,
        enabled: bool = False,
        refresh_minutes: int = 60,
        # Upcoming events within this horizon are shown in the prompt.
        lookahead_hours: float = 48.0,
        # Sent with every context HTTP request (SEC EDGAR rejects anonymous clients).
        http_user_agent: str = "trading-agent/0.1 (paper-trading research)",
        http_timeout_seconds: float = 15.0,
        sentiment: dict[str, Any] | None = None,
        macro: dict[str, Any] | None = None,
        announcements: dict[str, Any] | None = None,
        earnings: dict[str, Any] | None = None,
        news: dict[str, Any] | None = None,
        summarizer: dict[str, Any] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.refresh_minutes = int(_positive("context.refresh_minutes", refresh_minutes))
        self.lookahead_hours = _positive("context.lookahead_hours", lookahead_hours)
        self.http_user_agent = str(http_user_agent)
        self.http_timeout_seconds = _positive("context.http_timeout_seconds", http_timeout_seconds)
        self.sentiment = SentimentSettings(**(sentiment or {}))
        self.macro = MacroContextSettings(**(macro or {}))
        self.announcements = AnnouncementsSettings(**(announcements or {}))
        self.earnings = EarningsSettings(**(earnings or {}))
        self.news = NewsSettings(**(news or {}))
        self.summarizer = SummarizerSettings(**(summarizer or {}))
        if self.summarizer.enabled and not self.news.enabled:
            raise ValueError("context.summarizer needs context.news (it digests news items)")


class MacroCalendarSettings:
    """Scheduled macro events shared by both agents (§7.18, option (c)).

    ``events`` is the reliable base — an operator-maintained list (FOMC, CPI, NFP,
    ECB …, times in UTC) that works offline. ``feed_url`` adds the ForexFactory weekly
    JSON on top when reachable (unofficial; "" disables it). Each agent picks the
    currencies/importance it cares about in ``<agent>.context.macro``.
    """

    def __init__(
        self,
        feed_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.feed_url = feed_url or ""
        parsed: list[dict[str, Any]] = []
        for body in events or []:
            if not isinstance(body, dict):
                raise ValueError("macro_calendar.events entries must be mappings")  # noqa: TRY004
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
        self.events: list[dict[str, Any]] = parsed


#: Prompt playbooks a sleeve may use (§7.71) — texts live in
#: :data:`src.analysis.prompt_builder.PLAYBOOKS` (pinned equal by a test).
SLEEVE_PLAYBOOKS: tuple[str, ...] = ("swing", "position")

#: Sleeve names become the ``strategy`` column (String(20)) on decisions/orders.
_SLEEVE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,19}$")


class SleeveSpec:
    """One strategy sleeve (§7.71, CHANGE.md §4.1): a named trading style.

    A sleeve is a configuration of the decision pipeline — its own candle
    ``timeframe``, prompt ``playbook``, holding limit (time stop), per-sleeve risk
    limits and a fixed capital ``weight`` — not a new process.
    """

    def __init__(
        self,
        name: str,
        timeframe: str,
        playbook: str = "swing",
        # Time stop (deterministic exit next to SL/TP): close a position this sleeve
        # opened once it has been held longer than this. Omit for no time stop.
        holding: dict[str, float] | None = None,
        # Fixed share of agent equity allocated to this sleeve (allocator comes later).
        weight: float | None = None,
        # Per-sleeve risk limits: any RiskSettings field except enforce_exit_levels,
        # layered over the agent-level ``risk:`` block (CHANGE.md §4.8).
        risk: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(name, str) or not _SLEEVE_NAME_RE.match(name):
            raise ValueError(
                f"sleeve name {name!r} must be lowercase letters/digits/underscores, "
                "starting with a letter, at most 20 characters"
            )
        if not isinstance(timeframe, str) or timeframe_delta(timeframe) is None:
            raise ValueError(f"sleeves.{name}.timeframe {timeframe!r} is not a candle timeframe")
        if playbook not in SLEEVE_PLAYBOOKS:
            raise ValueError(
                f"sleeves.{name}.playbook must be one of {', '.join(SLEEVE_PLAYBOOKS)}"
            )
        holding = dict(holding or {})
        unknown = set(holding) - {"max_hours", "max_days"}
        if unknown:
            raise ValueError(f"sleeves.{name}.holding: unknown keys {sorted(unknown)}")
        if len(holding) > 1:
            raise ValueError(f"sleeves.{name}.holding: give max_hours OR max_days, not both")
        max_hours: float | None = None
        if "max_hours" in holding:
            max_hours = float(holding["max_hours"])
        elif "max_days" in holding:
            max_hours = float(holding["max_days"]) * 24.0
        if max_hours is not None and max_hours <= 0:
            raise ValueError(f"sleeves.{name}.holding must be > 0")
        if weight is not None and not 0.0 < float(weight) <= 1.0:
            raise ValueError(f"sleeves.{name}.weight must be in (0, 1]")
        risk = dict(risk or {})
        allowed = set(RiskSettings().__dict__) - {"enforce_exit_levels"}
        bad = set(risk) - allowed
        if bad:
            raise ValueError(
                f"sleeves.{name}.risk: unknown or non-overridable fields {sorted(bad)}"
            )
        self.name = name
        self.timeframe = timeframe
        self.playbook = playbook
        self.max_holding_hours = max_hours
        self.weight = float(weight) if weight is not None else None
        self.risk_overrides: dict[str, Any] = risk


class SleevesSettings:
    """Strategy sleeves for one agent (§7.71). Opt-in: ``enabled: false`` ships.

    With it off the agent runs exactly one implicit style on ``<agent>.timeframe``.
    When on, every sleeve decides on its own timeframe with its own playbook; a
    symbol is held by at most one sleeve at a time (symbol lock), and sleeves are
    evaluated in config order — the first listed wins a same-cycle tie.
    """

    def __init__(
        self,
        enabled: bool = False,
        # Loose agent-wide breaker (CHANGE.md §4.8): agent equity this far below its
        # peak blocks new entries in every sleeve, whatever the sleeves' own limits.
        backstop_max_drawdown_pct: float = 0.20,
        strategies: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if not 0.0 < float(backstop_max_drawdown_pct) < 1.0:
            raise ValueError("sleeves.backstop_max_drawdown_pct must be in (0, 1)")
        specs: list[SleeveSpec] = []
        for name, body in (strategies or {}).items():
            if not isinstance(body, dict):
                raise ValueError(f"sleeves.strategies.{name} must be a mapping")  # noqa: TRY004
            specs.append(SleeveSpec(name=name, **body))
        if enabled and not specs:
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
        self.enabled = bool(enabled)
        self.backstop_max_drawdown_pct = float(backstop_max_drawdown_pct)
        self.strategies: list[SleeveSpec] = specs

    def get(self, name: str | None) -> SleeveSpec | None:
        """The sleeve called ``name`` (``None`` when unknown)."""
        return next((s for s in self.strategies if s.name == name), None)


class RiskSettings:
    def __init__(
        self,
        max_position_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.02,
        max_drawdown_pct: float = 0.05,
        consecutive_losses_cooldown_minutes: int = 60,
        # Streak that arms the cooldown; was hardcoded to 3 in the tracker (§7.19).
        consecutive_losses_threshold: int = 3,
        max_open_positions: int = 5,
        min_confidence: float = 0.6,
        # §7.54 entry geometry: a BUY's stop must sit BELOW the current price and
        # within this fraction of it — an SL at 0.01 means unbounded risk, and stops
        # beyond the bar would auto-close next cycle for double fees.
        max_stop_distance_pct: float = 0.25,
        # §7.54 optional risk-per-trade sizing: cap a BUY so that
        # (entry − stop) × quantity ≤ this fraction of total value. 0 disables it.
        risk_per_trade_pct: float = 0.0,
        # Deterministic stop-loss / take-profit enforcement (§7.9): when enabled,
        # the pipeline closes a position as soon as its mark price breaches the
        # levels carried from the entry signal, without asking the LLM.
        enforce_exit_levels: bool = True,
        # §7.18 deterministic event guard (entries only — exits are never gated):
        # no new BUY in the window around a scheduled high-impact macro event, around
        # the symbol's earnings, or after the venue announced its delisting. Needs
        # ``<agent>.context.enabled`` — without context there are no events to check.
        event_guard_enabled: bool = True,
        event_blackout_before_minutes: int = 120,
        event_blackout_after_minutes: int = 60,
        event_guard_min_importance: str = "high",
        earnings_blackout_days_before: int = 1,
        earnings_blackout_hours_after: int = 24,
        delisting_blackout_days: int = 90,
    ) -> None:
        for name, value in (
            ("event_blackout_before_minutes", event_blackout_before_minutes),
            ("event_blackout_after_minutes", event_blackout_after_minutes),
            ("earnings_blackout_days_before", earnings_blackout_days_before),
            ("earnings_blackout_hours_after", earnings_blackout_hours_after),
            ("delisting_blackout_days", delisting_blackout_days),
        ):
            if int(value) < 0:
                raise ValueError(f"risk.{name} must be >= 0")
        if event_guard_min_importance not in IMPORTANCE_LEVELS:
            raise ValueError(
                f"risk.event_guard_min_importance must be one of {', '.join(IMPORTANCE_LEVELS)}"
            )
        self.max_position_pct = max_position_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.consecutive_losses_cooldown_minutes = consecutive_losses_cooldown_minutes
        self.consecutive_losses_threshold = consecutive_losses_threshold
        self.max_open_positions = max_open_positions
        self.min_confidence = min_confidence
        self.max_stop_distance_pct = max_stop_distance_pct
        self.risk_per_trade_pct = risk_per_trade_pct
        self.enforce_exit_levels = enforce_exit_levels
        self.event_guard_enabled = bool(event_guard_enabled)
        self.event_blackout_before_minutes = int(event_blackout_before_minutes)
        self.event_blackout_after_minutes = int(event_blackout_after_minutes)
        self.event_guard_min_importance = event_guard_min_importance
        self.earnings_blackout_days_before = int(earnings_blackout_days_before)
        self.earnings_blackout_hours_after = int(earnings_blackout_hours_after)
        self.delisting_blackout_days = int(delisting_blackout_days)


class ExecutionSettings:
    """Parameters for the simulated (paper) executor.

    Kept config-driven rather than hardcoded so paper PnL — which the LLM is
    shown and the live-readiness gates are compared against — reflects real
    trading costs. Set all to ``0`` to model a fee-free, slip-free venue.

    **Per-venue cost profiles (§7.65):** the flat ``paper_*`` fields are the
    default schedule; ``paper_costs`` overrides them per runner component
    (``crypto`` / ``stocks``), because each paper book should simulate the
    venue it stands in for — OKX EU spot (taker 0.20% as the account reports it,
    §7.75; no minimum) vs Saxo US
    stocks (0.08% with a **min $1/side** plus 0.25% FX). ``paper_costs_for()``
    resolves the effective schedule; runners and the backtester use it so
    paper numbers are honest for the §4.3 gates.
    """

    #: Overridable per-venue cost fields (§7.65).
    _COST_FIELDS = (
        "paper_fee_pct",
        "paper_slippage_pct",
        "paper_min_commission",
        "paper_fx_fee_pct",
    )

    def __init__(
        self,
        paper_fee_pct: float = 0.0,
        paper_slippage_pct: float = 0.001,  # per-side slippage (0.1%)
        initial_cash: float = 100_000.0,
        # Absolute commission floor per side, in the paper book's settlement
        # currency (§7.65). 0 = percentage-only venue.
        paper_min_commission: float = 0.0,
        # Applied per side when the chosen profile models a venue whose trades
        # settle in a currency other than the account's (Saxo: 0.25% EUR↔USD).
        paper_fx_fee_pct: float = 0.0,
        # Per-component overrides, e.g. {"stocks": {"paper_min_commission": 1.0}}.
        paper_costs: dict[str, dict] | None = None,
    ) -> None:
        self.paper_fee_pct = paper_fee_pct
        self.paper_slippage_pct = paper_slippage_pct
        self.paper_min_commission = float(paper_min_commission)
        self.paper_fx_fee_pct = float(paper_fx_fee_pct)
        # Starting bankroll for a *fresh* paper portfolio (§7.7: was hardcoded
        # in PaperExecutor). After the first cycle the persisted snapshot
        # wins — this only seeds an empty one.
        self.initial_cash = initial_cash
        resolved: dict[str, dict[str, float]] = {}
        for agent, overrides in (paper_costs or {}).items():
            if not isinstance(agent, str) or not isinstance(overrides, dict):
                # ValueError (not TypeError): these arrive from YAML, where a bad
                # shape is a config error with an actionable message (§7.65).
                raise ValueError(  # noqa: TRY004
                    "execution.paper_costs must map component names to dicts of cost fields"
                )
            clean: dict[str, float] = {}
            for field, value in overrides.items():
                if field not in self._COST_FIELDS:
                    raise ValueError(
                        f"execution.paper_costs.{agent}: unknown field {field!r} "
                        f"(allowed: {', '.join(self._COST_FIELDS)})"
                    )
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    raise ValueError(
                        f"execution.paper_costs.{agent}.{field} must be a non-negative number"
                    )
                clean[field] = float(value)
            resolved[agent] = clean
        self.paper_costs: dict[str, dict[str, float]] = resolved

    def paper_cost_params(self, agent: str | None = None) -> dict[str, float]:
        """Effective cost params for ``agent``: flat defaults merged with its profile."""
        params = {field: getattr(self, field) for field in self._COST_FIELDS}
        params.update(self.paper_costs.get(agent or "", {}))
        return params


class StorageSettings:
    """Where the per-agent × mode SQLite files live, and how they are kept (§7.78).

    ``data_dir`` holds ``<mode>_<agent>.db`` files (:mod:`src.core.db_layout`) — the
    runner derives the file from the executor's venue, never from config. The legacy
    ``database_path`` is still read: its parent directory becomes ``data_dir`` (and it
    is the default source of ``scripts/split_database.py``); ``":memory:"`` keeps one
    in-memory database (tests).
    """

    def __init__(
        self,
        database_path: str | None = None,
        data_dir: str | None = None,
        # Retention windows in days (§7.12). Market snapshots are the space hogs
        # (~100-candle JSON per symbol-cycle) and are re-creatable cache, so they
        # prune by default; decisions/orders are the trade record and kept forever
        # unless explicitly bounded. portfolio_snapshots are never pruned (the
        # drawdown high-water seed reads MAX over their full history).
        snapshot_retention_days: int = 30,
        history_retention_days: int = 0,
        # How often the runners run the pruning job while alive.
        prune_interval_minutes: int = 1440,
        # Point-in-time DB backups ahead of every prune pass (§7.35). Empty string
        # disables them; otherwise each pass writes ``<dir>/<db-stem>-<UTC stamp>.db``
        # via SQLite's online backup API BEFORE any rows are deleted.
        backup_dir: str = "",
        # How many backups to keep (oldest rotated out); 0 keeps everything.
        backup_keep: int = 0,
        # Market-context rows (§7.18: events, sentiment, news, context cards) older
        # than this are pruned; 0 keeps them forever.
        context_retention_days: int = 30,
    ) -> None:
        self.database_path = database_path
        self.in_memory = database_path == ":memory:"
        if data_dir:
            self.data_dir = str(data_dir)
        elif database_path and not self.in_memory:
            self.data_dir = str(Path(database_path).parent)
        else:
            self.data_dir = "data"
        self.snapshot_retention_days = snapshot_retention_days
        self.history_retention_days = history_retention_days
        self.prune_interval_minutes = prune_interval_minutes
        self.backup_dir = backup_dir
        self.backup_keep = backup_keep
        if int(context_retention_days) < 0:
            raise ValueError("storage.context_retention_days must be >= 0")
        self.context_retention_days = int(context_retention_days)


class MonitoringSettings:
    def __init__(
        self,
        log_level: str = "INFO",
        alert_dedup_window_seconds: int = 300,
        # §7.51 webhook alert channel. The URL itself is a secret (tokens live in
        # it) and comes ONLY from the ALERT_WEBHOOK_URL environment variable.
        alert_webhook_format: str = "json",
        alert_min_severity: str = "warning",
    ) -> None:
        if alert_webhook_format not in ("json", "ntfy"):
            raise ValueError("monitoring.alert_webhook_format must be 'json' or 'ntfy'")
        if alert_min_severity not in ("info", "warning", "error"):
            raise ValueError("monitoring.alert_min_severity must be info, warning or error")
        self.log_level = log_level
        self.alert_dedup_window_seconds = alert_dedup_window_seconds
        self.alert_webhook_format = alert_webhook_format
        self.alert_min_severity = alert_min_severity


class ControlApiSettings:
    """Agent-side control API (§7.15 P2): off unless explicitly enabled.

    Binds to loopback by default — the dashboard shares the Docker network (or the
    same host), never the public internet. It exposes only safe config + control
    latches; credentials are structurally absent from every endpoint.
    """

    def __init__(
        self,
        enabled: bool = False,
        host: str = "127.0.0.1",
        crypto_port: int = 8101,
        stocks_port: int = 8102,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.crypto_port = crypto_port
        self.stocks_port = stocks_port
        # Extra Host names accepted besides loopback + ``host`` (§7.43 DNS-rebinding
        # guard) — e.g. the compose service name when the dashboard calls in.
        self.allowed_hosts = list(allowed_hosts or [])


class XTBExecutionSettings:
    """XTB demo execution via xAPI (§7.16): off unless explicitly enabled.

    When enabled (and ``XTB_ACCOUNT_ID`` + ``XTB_ACCOUNT_PASSWORD`` are set in the
    environment), the stocks runner wires :class:`XTBExecutor` over the real
    :class:`~src.execution.xtb_client.XApiClient` instead of the paper executor.
    Default stays paper. ``account_type`` is validated at startup; keep it on
    ``demo`` — live trading remains out of scope (and this block is deliberately
    **not** in the dashboard's safe-config whitelist: enabling real execution
    must never be a web-form click).
    """

    def __init__(
        self,
        enabled: bool = False,
        host: str = "wss://ws.xapi.pro",
        account_type: str = "demo",
        request_timeout_seconds: float = 10.0,
        symbol_map: dict[str, str] | None = None,
    ) -> None:
        if account_type not in ("demo", "real"):
            raise ValueError("xtb_execution.account_type must be 'demo' or 'real'")
        self.enabled = enabled
        self.host = host
        self.account_type = account_type
        self.request_timeout_seconds = float(request_timeout_seconds)
        # Data → xAPI symbol table (§7.59 L8), e.g. {"AAPL": "AAPL.US"}. Unmapped
        # symbols pass through unchanged. Must be one-to-one so positions map back.
        symbol_map = dict(symbol_map or {})
        for key, value in symbol_map.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value:
                raise ValueError("xtb_execution.symbol_map must map symbol strings to strings")
        if len(set(symbol_map.values())) != len(symbol_map):
            raise ValueError("xtb_execution.symbol_map must be one-to-one")
        self.symbol_map: dict[str, str] = symbol_map


class VenueOrderSettings:
    """How a keyed ccxt venue executor prices and ages its orders (§7.75).

    The pipeline hands every order a *reference* price — the snapshot's last close.
    Sent as-is, that limit is not marketable whenever the book sits on the other
    side of it (the first OKX demo BUY rested under the ask and never filled), and a
    stop-loss SELL in a falling market would rest while the price kept dropping. So
    the executor, not the pipeline, turns the reference into a venue order:

    * BUY (entry) → limit at ``close × (1 + entry_offset_pct)``: crosses the spread,
      still bounded (sizing reserves the offset, so cash can never be overspent);
    * SELL (every spot close, §7.47) → ``market`` by default — an exit must fill;
      ``limit`` prices it at ``close × (1 − exit_offset_pct)`` instead;
    * an order still working after ``order_ttl_seconds`` is cancelled at the venue
      (0 = never) — an exit is then re-placed at the next cycle's mark;
    * one ``fetch_order`` ``fill_confirm_delay_seconds`` after placing resolves the
      fill in the same cycle (OKX acknowledges ``create_order`` with an id only).

    Paper execution is unaffected (it fills at the reference price + modelled
    slippage). Venue plumbing, not a risk knob: outside the dashboard's safe-config.
    """

    EXIT_ORDER_TYPES = ("market", "limit")

    def __init__(
        self,
        entry_offset_pct: float = 0.002,
        exit_order_type: str = "market",
        exit_offset_pct: float = 0.005,
        order_ttl_seconds: float = 600.0,
        fill_confirm_delay_seconds: float = 1.0,
    ) -> None:
        if exit_order_type not in self.EXIT_ORDER_TYPES:
            raise ValueError("venue_orders.exit_order_type must be 'market' or 'limit'")
        for name, value in (
            ("entry_offset_pct", entry_offset_pct),
            ("exit_offset_pct", exit_offset_pct),
        ):
            if not 0 <= float(value) < 0.05:
                raise ValueError(f"venue_orders.{name} must be in [0, 0.05)")
        for name, value in (
            ("order_ttl_seconds", order_ttl_seconds),
            ("fill_confirm_delay_seconds", fill_confirm_delay_seconds),
        ):
            if float(value) < 0:
                raise ValueError(f"venue_orders.{name} must be non-negative seconds")
        self.entry_offset_pct = float(entry_offset_pct)
        self.exit_order_type = exit_order_type
        self.exit_offset_pct = float(exit_offset_pct)
        self.order_ttl_seconds = float(order_ttl_seconds)
        self.fill_confirm_delay_seconds = float(fill_confirm_delay_seconds)


class SaxoExecutionSettings:
    """Stocks execution on Saxo OpenAPI (§7.66): off unless explicitly enabled.

    When enabled AND ``SAXO_ACCESS_TOKEN`` is set, the stocks runner trades through
    :class:`~src.execution.saxo_executor.SaxoExecutor` — ``environment: sim`` (Saxo's
    free simulation account) by default; ``live`` additionally needs
    ``LIVE_TRADING_ACK`` (§7.41). Deliberately outside the dashboard's safe-config
    whitelist, like every venue switch.
    """

    def __init__(
        self,
        enabled: bool = False,
        environment: str = "sim",
        # Which account trades: an explicit AccountKey, else the only active account
        # in ``account_currency`` (US stocks need a USD account — one currency, §7.66).
        account_key: str | None = None,
        account_currency: str | None = "USD",
        # Data → Saxo symbol (§7.59 L8 pattern), e.g. {"AAPL": "AAPL:xnas"}; unmapped
        # symbols are accepted only when the instrument lookup is unambiguous.
        symbol_map: dict[str, str] | None = None,
        request_timeout_seconds: float = 10.0,
        # Waits (s) between fill polls right after placing; still working after the
        # last one → resolved by per-cycle reconciliation.
        fill_poll_delays: list[float] | None = None,
        # Share-quantity precision: 0 = whole shares.
        amount_decimals: int = 0,
        # §7.66 step 4: OAuth app (authorization code + rotating refresh tokens) for
        # unattended runs instead of the 24 h developer token.
        oauth: dict[str, Any] | None = None,
    ) -> None:
        if environment not in ("sim", "live"):
            raise ValueError("saxo_execution.environment must be 'sim' or 'live'")
        symbol_map = dict(symbol_map or {})
        for key, value in symbol_map.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value:
                raise ValueError("saxo_execution.symbol_map must map symbol strings to strings")
        if len(set(symbol_map.values())) != len(symbol_map):
            raise ValueError("saxo_execution.symbol_map must be one-to-one")
        delays = [0.5, 1.0, 2.0, 4.0] if fill_poll_delays is None else list(fill_poll_delays)
        if not delays or any(float(d) < 0 for d in delays):
            raise ValueError("saxo_execution.fill_poll_delays must be non-negative seconds")
        if not 0 <= int(amount_decimals) <= 8:
            raise ValueError("saxo_execution.amount_decimals must be between 0 and 8")
        self.enabled = bool(enabled)
        self.environment = environment
        self.account_key = account_key or None
        self.account_currency = account_currency.upper() if account_currency else None
        self.symbol_map: dict[str, str] = symbol_map
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.fill_poll_delays: tuple[float, ...] = tuple(float(d) for d in delays)
        self.amount_decimals = int(amount_decimals)
        self.oauth = SaxoOAuthSettings(**(oauth or {}))


class SaxoOAuthSettings:
    """Saxo OAuth app settings (§7.66 step 4). App key/secret come from the environment
    (``SAXO_APP_KEY`` / ``SAXO_APP_SECRET``); the token pair lives in ``token_file``."""

    def __init__(
        self,
        enabled: bool = False,
        # Must equal the app's registered redirect URL; scripts/saxo_login.py listens here.
        redirect_uri: str = "http://localhost:8765/callback",
        # None → sim.logonvalidation.net / live.logonvalidation.net by environment.
        auth_base_url: str | None = None,
        # None → <storage.data_dir>/saxo_<environment>.token.json (0600, gitignored).
        token_file: str | None = None,
        refresh_margin_seconds: float = 120.0,
        # Rotate the pair this often while running — SIM refresh tokens live 40 min
        # and the stocks agent makes no API calls outside market hours.
        keepalive_minutes: float = 10.0,
    ) -> None:
        if not str(redirect_uri).startswith(("http://", "https://")):
            raise ValueError("saxo_execution.oauth.redirect_uri must be an http(s) URL")
        if float(refresh_margin_seconds) < 0 or float(keepalive_minutes) < 0:
            raise ValueError("saxo_execution.oauth margins/intervals must be >= 0")
        self.enabled = bool(enabled)
        self.redirect_uri = str(redirect_uri)
        self.auth_base_url = auth_base_url or None
        self.token_file = token_file or None
        self.refresh_margin_seconds = float(refresh_margin_seconds)
        self.keepalive_minutes = float(keepalive_minutes)

    def token_path(self, data_dir: str, environment: str) -> Path:
        return Path(self.token_file or Path(data_dir) / f"saxo_{environment}.token.json")


class DashboardSettings:
    """Standalone web dashboard (§7.15 P3–P4): FastAPI + Jinja2/HTMX.

    Launched on its own (``scripts/run_dashboard.py``); opens every per-mode book
    (§7.78 ``open_books``) and reads each through its own storage
    DB as a reader (WAL mode lets it read while the agents write). Control actions
    write the ``agent_control`` latches directly — the same writes the agent-side
    control API makes — so they work whether or not ``control_api.enabled``.

    Binds to loopback by default; credentials are structurally absent from every
    page. The dashboard only ever shows/edits the safe config surface
    (:class:`~src.core.control_config.SafeConfigOverrides`).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        refresh_seconds: int = 5,
        agents: list[str] | None = None,
        allow_launch: bool = False,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        # HTMX polling interval for the live fragments (health cards, chart).
        self.refresh_seconds = max(1, refresh_seconds)
        # Which control rows to show/control. Defaults to both built-in agents.
        self.agents = agents if agents else ["crypto", "stocks"]
        # §7.24: opt-in process supervision — Start/Stop buttons that spawn/terminate
        # local `scripts.run_<agent>_agent` runners. Off by default; pointless (and
        # confusing) inside docker-compose, where services are managed by compose.
        self.allow_launch = bool(allow_launch)
        # Extra Host names the browser may use besides loopback + ``host`` (§7.43):
        # every request with any other Host header is rejected (DNS rebinding).
        self.allowed_hosts = list(allowed_hosts or [])
