"""Tests for the LM Studio LLM client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import LLMSettings
from src.core.llm_client import LLMClient, _parse_signal, _resolve_chat_url


@pytest.fixture()
def llm_settings() -> LLMSettings:
    return LLMSettings(
        endpoint="http://localhost:1234/v1/chat/completions",
        model="qwen3.6-27b-mtp",
        timeout_seconds=5,
        max_retries=2,
        # Keep retry tests instant; the backoff itself is asserted separately.
        retry_backoff_base_seconds=0.0,
    )


@pytest.fixture()
def client(llm_settings: LLMSettings) -> LLMClient:
    return LLMClient(llm_settings)


class TestParseSignal:
    def test_plain_json(self) -> None:
        raw = '{"symbol": "BTC/USDT", "action": "buy", "confidence": 0.85, "reasoning": "bullish"}'

        signal = _parse_signal(raw)
        assert signal.symbol == "BTC/USDT"
        assert signal.action.value == "buy"
        assert signal.confidence == 0.85

    def test_markdown_code_block(self) -> None:
        raw = '```json\n{"symbol": "ETH/USDT", "action": "sell", "confidence": 0.7, "reasoning": "bearish"}\n```'

        signal = _parse_signal(raw)
        assert signal.symbol == "ETH/USDT"
        assert signal.action.value == "sell"

    def test_code_block_without_lang(self) -> None:
        raw = '```\n{"symbol": "AAPL", "action": "hold", "confidence": 0.5, "reasoning": "neutral"}\n```'

        signal = _parse_signal(raw)
        assert signal.action.value == "hold"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _parse_signal("not json at all")

    def test_model_cannot_forge_fallback_flag(self) -> None:
        # is_fallback marks the client's own LLM-unavailable HOLDs; anything the
        # model puts there is ignored (§7.8 — audit rows must stay trustworthy).
        raw = '{"symbol": "BTC/USDT", "action": "hold", "confidence": 0.5, "reasoning": "x", "is_fallback": true}'
        assert _parse_signal(raw).is_fallback is False


def _resp(content: str) -> MagicMock:
    mock_response = MagicMock()
    mock_response.json.return_value = {"choices": [{"message": {"content": content}}]}
    mock_response.raise_for_status = MagicMock()
    return mock_response


class TestSeedAndSizeGuard:
    """§7.33: optional determinism seed + raw response size upper bound."""

    async def test_seed_omitted_by_default(self, client: LLMClient) -> None:
        post = AsyncMock(
            return_value=_resp(
                '{"symbol": "BTC/USDT", "action": "hold", "confidence": 0.5, "reasoning": "x"}'
            )
        )
        with patch.object(client._client, "post", new=post):
            await client.ask_trade_signal("s", "u")
        assert "seed" not in post.call_args.kwargs["json"]

    async def test_seed_sent_when_configured(self, llm_settings: LLMSettings) -> None:
        llm_settings.seed = 42
        client = LLMClient(llm_settings)
        post = AsyncMock(
            return_value=_resp(
                '{"symbol": "BTC/USDT", "action": "hold", "confidence": 0.5, "reasoning": "x"}'
            )
        )
        with patch.object(client._client, "post", new=post):
            await client.ask_trade_signal("s", "u")
        assert post.call_args.kwargs["json"]["seed"] == 42

    async def test_oversized_response_fails_attempt_then_falls_back(
        self, llm_settings: LLMSettings
    ) -> None:
        llm_settings.max_response_chars = 50
        client = LLMClient(llm_settings)
        huge = (
            '{"symbol": "BTC/USDT", "action": "buy", "confidence": 0.9, "reasoning": "'
            + "y" * 200
            + '"}'
        )
        post = AsyncMock(return_value=_resp(huge))
        with patch.object(client._client, "post", new=post):
            signal = await client.ask_trade_signal("s", "u")
        # Every attempt is rejected at the guard → safe HOLD, marked as fallback.
        assert post.await_count == llm_settings.max_retries
        assert signal.is_fallback is True
        assert signal.action.value == "hold"
        assert "too large" in (signal.reasoning or "")

    async def test_guard_disabled_with_zero(self, llm_settings: LLMSettings) -> None:
        llm_settings.max_response_chars = 0
        client = LLMClient(llm_settings)
        content = (
            '{"symbol": "BTC/USDT", "action": "buy", "confidence": 0.9, "reasoning": "'
            + "y" * 5000
            + '"}'
        )
        with patch.object(client._client, "post", new=AsyncMock(return_value=_resp(content))):
            signal = await client.ask_trade_signal("s", "u")
        assert signal.is_fallback is False
        assert len(signal.reasoning) > 4000


class TestAskTradeSignal:
    @pytest.mark.asyncio
    async def test_successful_call(self, client: LLMClient) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"symbol": "BTC/USDT", "action": "buy", "confidence": 0.9, "reasoning": "test", "stop_loss": 59000}'
                    }
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new=AsyncMock(return_value=mock_response)):
            signal = await client.ask_trade_signal(
                system_prompt="You are a trader.",
                user_prompt="Analyze BTC.",
            )

            assert signal.symbol == "BTC/USDT"
            assert signal.action.value == "buy"

    @pytest.mark.asyncio
    async def test_retries_on_failure(self, client: LLMClient) -> None:
        # First call fails, second succeeds
        mock_success = MagicMock()
        mock_success.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"symbol": "ETH/USDT", "action": "hold", "confidence": 0.5, "reasoning": "retry worked"}'
                    }
                }
            ]
        }
        mock_success.raise_for_status = MagicMock()

        with patch.object(
            client._client,
            "post",
            new=AsyncMock(side_effect=[Exception("timeout"), mock_success]),
        ):
            signal = await client.ask_trade_signal(
                system_prompt="You are a trader.",
                user_prompt="Analyze ETH.",
            )

            assert signal.symbol == "ETH/USDT"
            assert signal.action.value == "hold"

    @pytest.mark.asyncio
    async def test_fallback_hold_on_all_failures(self, client: LLMClient) -> None:
        with patch.object(
            client._client,
            "post",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ):
            signal = await client.ask_trade_signal(
                system_prompt="You are a trader.",
                user_prompt="Analyze BTC.",
            )

            assert signal.action.value == "hold"
            assert signal.confidence == 0.0
            assert "LLM unavailable" in signal.reasoning
            # Marked as a fallback so it is stored for audit but never re-fed
            # into later prompts as if the model had genuinely decided (§7.8).
            assert signal.is_fallback is True

    @pytest.mark.asyncio
    async def test_genuine_signal_is_not_marked_fallback(self, client: LLMClient) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"symbol": "BTC/USDT", "action": "buy", "confidence": 0.9, "reasoning": "test"}'
                    }
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new=AsyncMock(return_value=mock_response)):
            signal = await client.ask_trade_signal(
                system_prompt="You are a trader.",
                user_prompt="Analyze BTC.",
            )

        assert signal.is_fallback is False


class TestEndpointParsing:
    def test_base_url_extraction(self, llm_settings: LLMSettings) -> None:
        client = LLMClient(llm_settings)

        # The base URL should be the endpoint without /v1/chat/completions
        assert "localhost" in str(client._client.base_url)

    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            # Every shipped shape resolves to the full chat-completions URL —
            # no "/v1" string surgery (§7.19).
            (
                "http://localhost:1234/v1/chat/completions",
                "http://localhost:1234/v1/chat/completions",
            ),
            ("http://localhost:1234/v1/", "http://localhost:1234/v1/chat/completions"),
            ("http://localhost:1234", "http://localhost:1234/v1/chat/completions"),
            ("http://host:8080/custom/v1", "http://host:8080/custom/v1/chat/completions"),
        ],
    )
    def test_chat_url_resolution(self, endpoint: str, expected: str) -> None:
        settings = LLMSettings(endpoint=endpoint, model="m")
        assert _resolve_chat_url(settings.endpoint) == expected

    def test_client_posts_resolved_absolute_url(self, llm_settings: LLMSettings) -> None:
        client = LLMClient(llm_settings)
        assert client._chat_url == "http://localhost:1234/v1/chat/completions"


class TestRetryBackoff:
    @pytest.mark.asyncio
    async def test_exponential_sleep_between_attempts(self) -> None:
        settings = LLMSettings(
            endpoint="http://localhost:1234/v1/chat/completions",
            model="m",
            max_retries=3,
            retry_backoff_base_seconds=0.5,
        )
        client = LLMClient(settings)
        try:
            sleeps: list[float] = []

            async def record_sleep(delay: float) -> None:
                sleeps.append(delay)

            with (
                patch.object(client._client, "post", new=AsyncMock(side_effect=Exception("boom"))),
                patch("src.core.llm_client.asyncio.sleep", new=record_sleep),
            ):
                signal = await client.ask_trade_signal("s", "u")

            assert signal.is_fallback is True
            assert sleeps == [0.5, 1.0]  # base * 2^(attempt-1); none after the last attempt
        finally:
            await client.close()


class TestClose:
    @pytest.mark.asyncio
    async def test_closes_http_client(self, client: LLMClient) -> None:
        with patch.object(client._client, "aclose", new=AsyncMock()) as mock_close:
            await client.close()
            mock_close.assert_called_once()
