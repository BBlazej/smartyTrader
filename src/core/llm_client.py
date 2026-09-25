"""LM Studio HTTP client for structured LLM calls."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import structlog
from pydantic import ValidationError

from .config import LLMSettings
from .models import TradeSignal

# structlog like the rest of the codebase — safety-relevant LLM calls must land
# in the configured renderers, not bypass them via stdlib logging (§7.19 nit).
logger = structlog.get_logger()


# JSON schema for the TradeSignal. Sent as the ``response_format`` so the LLM
# returns a stable, parseable object (LM Studio supports ``json_schema``).
TRADE_SIGNAL_JSON_SCHEMA: dict[str, Any] = {
    "name": "trade_signal",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reasoning": {"type": "string"},
            "stop_loss": {"type": ["number", "null"]},
            "take_profit": {"type": ["number", "null"]},
        },
        "required": ["symbol", "action", "confidence", "reasoning"],
        "additionalProperties": False,
    },
}


class LLMClient:
    """Thin async client over LM Studio's OpenAI-compatible endpoint."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        # Resolve the chat-completions URL once instead of slicing the configured
        # endpoint per request: the old ``rsplit("/v1")`` + re-append dance worked
        # only for the shipped config shape (§7.19).
        self._chat_url = _resolve_chat_url(settings.endpoint)
        self._client = httpx.AsyncClient(
            base_url=_derive_base_url(settings.endpoint),
            timeout=settings.timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def ask_trade_signal(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
    ) -> TradeSignal:
        """Send a prompt and parse the response into a structured TradeSignal.

        Retries up to ``settings.max_retries`` on transient errors or JSON parse failures.
        Falls back to HOLD signal if all retries are exhausted.

        A strict ``TRADE_SIGNAL_JSON_SCHEMA`` is attached to the request when
        ``settings.use_json_schema`` is enabled (or when one is passed explicitly),
        which makes the response stable to parse.
        """
        last_error: Exception | None = None

        # Default to the shared schema when enabled, but allow an explicit override.
        effective_schema = (
            json_schema
            if json_schema is not None
            else (TRADE_SIGNAL_JSON_SCHEMA if self.settings.use_json_schema else None)
        )

        for attempt in range(1, self.settings.max_retries + 1):
            try:
                payload: dict[str, Any] = {
                    "model": self.settings.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": self.settings.temperature,
                    "max_tokens": self.settings.max_tokens,
                }

                # Optional determinism knob (§7.33) — omitted when unset so the
                # provider default applies.
                if self.settings.seed is not None:
                    payload["seed"] = self.settings.seed

                if effective_schema:
                    payload["response_format"] = {
                        "type": "json_schema",
                        "json_schema": effective_schema,
                    }

                resp = await self._client.post(self._chat_url, json=payload)
                resp.raise_for_status()

                data = resp.json()
                choice = data["choices"][0]
                raw_content = choice["message"]["content"]
                if choice.get("finish_reason") == "length":
                    # Cut off at max_tokens — usually a long reasoning block ate
                    # the budget before the JSON (§7.57). Name it in the log so
                    # the fix (raise max_tokens) is obvious.
                    logger.warning(
                        "llm_response_truncated",
                        max_tokens=self.settings.max_tokens,
                        attempt=attempt,
                    )

                # Size guard (§7.33): a runaway generation must fail the attempt
                # (retry → eventual HOLD fallback), never reach the parser.
                limit = self.settings.max_response_chars
                if limit > 0 and len(raw_content) > limit:
                    raise ValueError(
                        f"LLM response too large: {len(raw_content)} chars > limit {limit}"
                    )

                # Audit trail (§3.3 / §7.8): the *full* prompt + response behind
                # every live decision, not just the parsed action.
                logger.info(
                    "llm_exchange",
                    model=self.settings.model,
                    attempt=attempt,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response=raw_content,
                )

                signal = _parse_signal(raw_content)
                logger.debug("llm signal parsed", action=signal.action.value, attempt=attempt)
                return signal

            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "llm_attempt_failed",
                    attempt=attempt,
                    max_retries=self.settings.max_retries,
                    error=str(exc),
                )
                # Exponential backoff between attempts — hammering a struggling
                # local server on every retry helped nothing (§7.19).
                if attempt < self.settings.max_retries and self.settings.retry_backoff_base_seconds:
                    delay = self.settings.retry_backoff_base_seconds * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        # All retries exhausted — return safe HOLD fallback. Marked so it is
        # persisted for audit but never re-fed into later prompts (§7.8).
        logger.error("llm_retries_exhausted", last_error=str(last_error))
        return TradeSignal(
            symbol="UNKNOWN",
            action="hold",
            confidence=0.0,
            reasoning=f"LLM unavailable after {self.settings.max_retries} retries: {last_error}",
            is_fallback=True,
        )


def _resolve_chat_url(endpoint: str) -> str:
    """Normalize any of the shipped endpoint shapes to the full chat URL.

    Accepts ``.../v1/chat/completions``, ``.../v1`` or a bare ``host[:port]`` —
    all resolve to the OpenAI-compatible chat-completions path without string
    surgery on ``"/v1"`` (§7.19).
    """
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    if endpoint.endswith("/v1"):
        return f"{endpoint}/chat/completions"
    return f"{endpoint}/v1/chat/completions"


def _derive_base_url(endpoint: str) -> str:
    """Origin (+ optional path prefix) used for httpx connection pooling."""
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint[: -len("/chat/completions")] + "/"
    return endpoint + "/"


# Reasoning-model scratchpads (Qwen3, DeepSeek-R1 …). Some LM Studio builds put
# them in ``content`` ahead of the answer, so they must never reach ``json.loads``
# (§7.57). Matched case-insensitively; ``<thinking>`` is a common variant.
_THINK_BLOCK_RE = re.compile(r"<(think|thinking)>.*?</\1>", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE_RE = re.compile(r"</(?:think|thinking)>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<(?:think|thinking)>", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Drop reasoning blocks: closed ones, an unopened preamble, an unclosed tail."""
    text = _THINK_BLOCK_RE.sub("", text)
    # Chat templates that open the think block themselves emit only the closing
    # tag — everything before the last one is reasoning.
    closes = list(_THINK_CLOSE_RE.finditer(text))
    if closes:
        text = text[closes[-1].end() :]
    # A block still open was cut off (``max_tokens``): a JSON *draft* inside it
    # is not an answer and must never be traded on.
    opened = _THINK_OPEN_RE.search(text)
    if opened:
        text = text[: opened.start()]
    return text.strip()


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Every top-level, balanced ``{…}`` object in ``text``, in order.

    ``raw_decode`` does the balancing, so braces inside JSON strings (a
    ``reasoning`` quoting ``{x}``) cannot derail it; nested objects are skipped
    because the scan resumes after each decoded top-level object.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = text.find("{", end)
    return found


def _parse_signal(raw: str) -> TradeSignal:
    """Extract the trade-signal JSON from an LLM response and validate it.

    Tolerates reasoning blocks, markdown fences and prose around the object
    (§7.57): the *last* balanced object that validates as a ``TradeSignal``
    wins — models that restate or correct themselves put the final answer last.
    Raises ``json.JSONDecodeError`` when no object is present at all (e.g. a
    reasoning block truncated by ``max_tokens``) and the validation error of the
    last candidate when none validates, so the caller's retry path runs.
    """
    text = _strip_reasoning(raw)
    candidates = _json_objects(text)
    if not candidates:
        raise json.JSONDecodeError("No JSON object in LLM response", text, 0)

    last_error: Exception | None = None
    for data in reversed(candidates):
        # The fallback marker is ours alone — never let model output forge it.
        data.pop("is_fallback", None)
        try:
            return TradeSignal(**data)
        except (ValidationError, TypeError) as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
