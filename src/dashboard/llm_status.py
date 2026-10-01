"""The overview's LLM status card: is the local LLM server up, and how is it doing?

Two sources. A live probe of the server's OpenAI-compatible ``GET /v1/models`` (every
compatible server has it; Unsloth Studio also says whether the model is *loaded* and
its context length), cached for ``ttl_seconds`` so HTMX polling never hammers it.
And the per-decision numbers the agent already stores (§7.69): response time, speed,
tokens, fallbacks — read by :meth:`Storage.get_llm_latency_stats`.

Read-only: the probe never sends a prompt, and the API key (if any) only travels in
the ``Authorization`` header. The card shows no ``llm.*`` value — not the model name,
not the endpoint (the dashboard's rule, ``control_config.py``) — only what they do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from ..core.config import LLMSettings
from ..core.llm_client import _resolve_chat_url

logger = structlog.get_logger()


@dataclass(frozen=True)
class LLMProbe:
    """One look at the LLM server."""

    reachable: bool
    model: str
    #: Is the configured model among the served ones (None: the server listed none).
    model_listed: bool | None = None
    #: Unsloth Studio reports whether it is loaded; other servers say nothing (None).
    model_loaded: bool | None = None
    context_length: int | None = None
    probe_ms: float | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        """``online`` / ``offline`` / ``not loaded`` / ``model missing``."""
        if not self.reachable:
            return "offline"
        if self.model_listed is False:
            return "model missing"
        if self.model_loaded is False:
            return "not loaded"
        return "online"


def models_url(endpoint: str) -> str:
    """The ``/models`` URL next to the chat-completions URL of any endpoint shape."""
    return _resolve_chat_url(endpoint)[: -len("/chat/completions")] + "/models"


def parse_models(payload: Any, model: str) -> tuple[bool | None, bool | None, int | None]:
    """``(listed, loaded, context_length)`` of ``model`` in a ``/models`` answer."""
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        return None, None, None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == model:
            loaded = entry.get("loaded")
            context = entry.get("context_length")
            return (
                True,
                loaded if isinstance(loaded, bool) else None,
                int(context) if isinstance(context, int | float) else None,
            )
    return False, None, None


class LLMProbeCache:
    """Probe the server at most once per ``ttl_seconds``."""

    def __init__(
        self,
        settings: LLMSettings,
        ttl_seconds: float = 15.0,
        timeout_seconds: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._transport = transport
        self._url = models_url(settings.endpoint)
        self._last: LLMProbe | None = None
        self._last_at = 0.0

    def peek(self) -> LLMProbe | None:
        """The last probe result without probing (``None`` before the first one)."""
        return self._last

    async def get(self) -> LLMProbe:
        if self._last is not None and time.monotonic() - self._last_at < self._ttl:
            return self._last
        self._last = await self._probe()
        self._last_at = time.monotonic()
        return self._last

    async def _probe(self) -> LLMProbe:
        model = self._settings.model
        api_key = self._settings.api_key
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=headers, transport=self._transport
            ) as client:
                resp = await client.get(self._url)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - a status card must never fail the page
            return LLMProbe(reachable=False, model=model, error=_short_error(exc))
        listed, loaded, context = parse_models(payload, model)
        return LLMProbe(
            reachable=True,
            model=model,
            model_listed=listed,
            model_loaded=loaded,
            context_length=context,
            probe_ms=(time.perf_counter() - started) * 1000.0,
        )


def _short_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.ConnectError):
        return "connection refused"
    return type(exc).__name__


def llm_status_view(
    probe: LLMProbe, stats: dict[str, Any] | None, max_tokens: int
) -> dict[str, Any]:
    """Everything the card shows, pre-shaped (seconds, k-tokens, warnings)."""
    stats = stats or {}

    def seconds(key: str) -> float | None:
        value = stats.get(key)
        return round(value / 1000.0, 1) if value is not None else None

    budget = None
    if stats.get("max_prompt_tokens") is not None:
        budget = int(stats["max_prompt_tokens"]) + max_tokens
    return {
        "probe": probe,
        "status": probe.status,
        "count": int(stats.get("count") or 0),
        "avg_s": seconds("avg_ms"),
        "p50_s": seconds("p50_ms"),
        "p95_s": seconds("p95_ms"),
        "max_s": seconds("max_ms"),
        "tokens_per_s": (
            round(stats["tokens_per_s"], 1) if stats.get("tokens_per_s") is not None else None
        ),
        "avg_completion_tokens": stats.get("avg_completion_tokens"),
        "max_completion_tokens": stats.get("max_completion_tokens"),
        "avg_prompt_tokens": stats.get("avg_prompt_tokens"),
        "max_prompt_tokens": stats.get("max_prompt_tokens"),
        "max_tokens": max_tokens,
        "fallbacks": int(stats.get("fallbacks") or 0),
        "last_at": stats.get("last_at"),
        # Worst prompt seen + the answer cap must fit the loaded context window.
        "context_overflow": bool(
            budget is not None and probe.context_length and budget > probe.context_length
        ),
    }


#: A full answer must fit the timeout with this much room (prompt processing, load).
TIMEOUT_MARGIN = 1.2


def llm_budget(stats: dict[str, Any] | None, probe: LLMProbe | None, max_tokens: int) -> dict:
    """What the measured numbers say about an ``llm.max_tokens`` choice (§7.91).

    ``min_timeout_s``: a full ``max_tokens`` answer at the measured speed, with margin.
    ``context_needed``: the largest prompt seen + the answer cap, against the context
    the server reports (Unsloth Studio). ``None`` wherever the data is missing.
    """
    stats = stats or {}
    speed = stats.get("tokens_per_s")
    max_prompt = stats.get("max_prompt_tokens")
    context = probe.context_length if probe is not None else None
    return {
        "tokens_per_s": round(speed, 1) if speed else None,
        "min_timeout_s": int(max_tokens / speed * TIMEOUT_MARGIN) + 1 if speed else None,
        "max_prompt_tokens": max_prompt,
        "context_length": context,
        "context_needed": (int(max_prompt) + max_tokens) if max_prompt is not None else None,
    }


def llm_budget_problem(max_tokens: int, timeout_seconds: int, budget: dict[str, Any]) -> str | None:
    """Why an LLM override would break decisions, or ``None``.

    Too short a timeout turns every long answer into a fallback HOLD; a prompt + cap
    beyond the loaded context makes the server refuse or truncate.
    """
    if budget.get("min_timeout_s") and timeout_seconds < budget["min_timeout_s"]:
        return (
            f"llm.timeout_seconds={timeout_seconds} is shorter than a full {max_tokens}-token "
            f"answer at the measured {budget['tokens_per_s']} tok/s — raise it to at least "
            f"{budget['min_timeout_s']} s or lower max_tokens"
        )
    needed, context = budget.get("context_needed"), budget.get("context_length")
    if needed and context and needed > context:
        return (
            f"llm.max_tokens={max_tokens} plus the largest prompt seen "
            f"({budget['max_prompt_tokens']} tokens) needs {needed} tokens of context; the "
            f"server has {context} loaded"
        )
    return None
