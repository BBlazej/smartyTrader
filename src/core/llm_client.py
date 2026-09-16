"""LM Studio HTTP client for structured LLM calls."""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

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
        self._client = httpx.AsyncClient(
            base_url=settings.endpoint.rsplit("/v1", 1)[0],
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
                    "temperature": 0.2,
                    "max_tokens": 1024,
                }

                if effective_schema:
                    payload["response_format"] = {
                        "type": "json_schema",
                        "json_schema": effective_schema,
                    }

                resp = await self._client.post(
                    "/v1/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()

                data = resp.json()
                raw_content = data["choices"][0]["message"]["content"]

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


def _parse_signal(raw: str) -> TradeSignal:
    """Extract JSON from LLM response and validate into TradeSignal."""
    text = raw.strip()

    # Handle markdown code blocks (with or without language tag like ```json)
    if "```" in text:
        start = text.find("```") + 3
        # Skip the language tag if present (e.g. "json\n")
        first_newline = text[start:].find("\n")
        if first_newline != -1:
            start += first_newline + 1
        end = text.rfind("```")
        text = text[start:end].strip()

    data = json.loads(text)
    # The fallback marker is ours alone — never let model output forge it.
    data.pop("is_fallback", None)
    return TradeSignal(**data)
