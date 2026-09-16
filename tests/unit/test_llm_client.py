"""Tests for the LM Studio LLM client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import LLMSettings
from src.core.llm_client import LLMClient, _parse_signal


@pytest.fixture()
def llm_settings() -> LLMSettings:
    return LLMSettings(
        endpoint="http://localhost:1234/v1/chat/completions",
        model="qwen3.6-27b-mtp",
        timeout_seconds=5,
        max_retries=2,
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


class TestClose:
    @pytest.mark.asyncio
    async def test_closes_http_client(self, client: LLMClient) -> None:
        with patch.object(client._client, "aclose", new=AsyncMock()) as mock_close:
            await client.close()
            mock_close.assert_called_once()
