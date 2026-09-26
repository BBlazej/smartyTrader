"""Tests for the LM Studio LLM client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

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


_SIG = '{"symbol": "BTC/USDT", "action": "buy", "confidence": 0.8, "reasoning": "breakout"}'


class TestParseSignalReasoningModels:
    """§7.57: think blocks, prose and multiple objects around the answer."""

    def test_think_block_before_json(self) -> None:
        raw = "<think>\nRSI is 72, maybe {overbought}? Let me weigh it.\n</think>\n\n" + _SIG
        assert _parse_signal(raw).action.value == "buy"

    def test_think_block_with_draft_json_is_ignored(self) -> None:
        draft = '{"symbol": "BTC/USDT", "action": "sell", "confidence": 0.9, "reasoning": "d"}'
        raw = f"<think>First idea: {draft}. No wait.</think>{_SIG}"
        assert _parse_signal(raw).action.value == "buy"

    def test_unopened_close_tag_drops_preamble(self) -> None:
        # Templates that open the think block themselves emit only "</think>".
        draft = '{"symbol": "BTC/USDT", "action": "sell", "confidence": 0.9, "reasoning": "d"}'
        raw = f"reasoning here {draft} more reasoning</think>\n{_SIG}"
        assert _parse_signal(raw).action.value == "buy"

    def test_truncated_think_block_never_yields_a_draft(self) -> None:
        # max_tokens cut the reasoning off: a JSON draft inside it is no answer.
        raw = "<think>Maybe " + _SIG + " but let me also check the MACD and"
        with pytest.raises(json.JSONDecodeError):
            _parse_signal(raw)

    def test_leading_and_trailing_prose(self) -> None:
        raw = f"Here is my decision:\n{_SIG}\nHope this helps!"
        assert _parse_signal(raw).confidence == 0.8

    def test_braces_inside_strings_do_not_break_extraction(self) -> None:
        raw = (
            'Answer: {"symbol": "BTC/USDT", "action": "hold", "confidence": 0.4, '
            '"reasoning": "range {60k-62k} holds; no } breakout"}'
        )
        assert _parse_signal(raw).reasoning == "range {60k-62k} holds; no } breakout"

    def test_last_valid_object_wins(self) -> None:
        first = '{"symbol": "BTC/USDT", "action": "sell", "confidence": 0.6, "reasoning": "a"}'
        raw = f"Initially: {first}\nCorrected final answer: {_SIG}"
        assert _parse_signal(raw).action.value == "buy"

    def test_non_signal_trailing_object_falls_back_to_earlier_signal(self) -> None:
        raw = f'{_SIG}\nMetadata: {{"tokens": 12}}'
        assert _parse_signal(raw).action.value == "buy"

    def test_no_valid_signal_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            _parse_signal('{"action": "moon", "confidence": 2}')

    def test_fence_after_think_block(self) -> None:
        raw = f"<think>hmm</think>\n```json\n{_SIG}\n```"
        assert _parse_signal(raw).symbol == "BTC/USDT"


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

    async def test_truncated_completion_is_logged_and_falls_back(self, client: LLMClient) -> None:
        resp = MagicMock()
        resp.json.return_value = {
            "choices": [
                {"message": {"content": "<think>long reasoning"}, "finish_reason": "length"}
            ]
        }
        resp.raise_for_status = MagicMock()
        with (
            patch.object(client._client, "post", new=AsyncMock(return_value=resp)),
            patch("src.core.llm_client.logger") as log,
        ):
            signal = await client.ask_trade_signal("s", "u")
        assert signal.is_fallback is True
        events = [c.args[0] for c in log.warning.call_args_list]
        assert "llm_response_truncated" in events


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


def _resp_usage(content: str, usage: dict | None) -> MagicMock:
    mock_response = MagicMock()
    payload: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    mock_response.json.return_value = payload
    mock_response.raise_for_status = MagicMock()
    return mock_response


class TestCallMetrics:
    """§7.69: every ask_trade_signal records latency + token usage."""

    async def test_successful_call_records_usage(self, client: LLMClient) -> None:
        with patch.object(
            client._client,
            "post",
            new=AsyncMock(
                return_value=_resp_usage(
                    _SIG, {"prompt_tokens": 1234, "completion_tokens": 210, "total_tokens": 1444}
                )
            ),
        ):
            await client.ask_trade_signal("sys", "user")

        metrics = client.last_metrics
        assert metrics is not None
        assert metrics.attempts == 1
        assert metrics.latency_ms > 0
        assert metrics.prompt_tokens == 1234
        assert metrics.completion_tokens == 210

    async def test_missing_usage_block_records_none_tokens(self, client: LLMClient) -> None:
        with patch.object(client._client, "post", new=AsyncMock(return_value=_resp(_SIG))):
            await client.ask_trade_signal("sys", "user")
        metrics = client.last_metrics
        assert metrics is not None
        assert metrics.prompt_tokens is None
        assert metrics.completion_tokens is None

    async def test_garbage_usage_block_tolerated(self, client: LLMClient) -> None:
        resp = _resp_usage(_SIG, {"prompt_tokens": "nonsense", "completion_tokens": None})
        with patch.object(client._client, "post", new=AsyncMock(return_value=resp)):
            await client.ask_trade_signal("sys", "user")
        assert client.last_metrics.prompt_tokens is None

    async def test_fallback_metrics_count_all_attempts(self, client: LLMClient) -> None:
        with patch.object(
            client._client, "post", new=AsyncMock(side_effect=Exception("connection refused"))
        ):
            signal = await client.ask_trade_signal("sys", "user")
        assert signal.is_fallback is True
        metrics = client.last_metrics
        assert metrics is not None
        assert metrics.attempts == 2  # settings.max_retries
        assert metrics.latency_ms > 0  # includes the failed attempts + backoff
        assert metrics.prompt_tokens is None
