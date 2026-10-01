"""The overview's LLM status card: server probe, status, cached polling, view shaping."""

from __future__ import annotations

import httpx
import pytest

from src.core.config import LLMSettings
from src.dashboard.llm_status import (
    LLMProbe,
    LLMProbeCache,
    llm_status_view,
    models_url,
    parse_models,
)

MODEL = "unsloth/Qwen3.8-27B-GGUF"
STUDIO = {
    "object": "list",
    "data": [
        {"id": MODEL, "loaded": True, "context_length": 70000},
        {"id": "other", "loaded": False},
    ],
}


def settings() -> LLMSettings:
    return LLMSettings(endpoint="http://localhost:8889/v1", model=MODEL)


def test_models_url_from_any_endpoint_shape() -> None:
    assert models_url("http://h:8889/v1") == "http://h:8889/v1/models"
    assert models_url("http://h:1234/v1/chat/completions") == "http://h:1234/v1/models"
    assert models_url("http://h:1234") == "http://h:1234/v1/models"


def test_parse_models() -> None:
    assert parse_models(STUDIO, MODEL) == (True, True, 70000)
    assert parse_models(STUDIO, "missing") == (False, None, None)
    # Plain OpenAI-compatible servers (llama-server, LM Studio): listed, nothing else.
    assert parse_models({"data": [{"id": MODEL}]}, MODEL) == (True, None, None)
    assert parse_models({"data": []}, MODEL) == (None, None, None)
    assert parse_models("garbage", MODEL) == (None, None, None)


def test_status() -> None:
    assert LLMProbe(reachable=False, model=MODEL).status == "offline"
    assert LLMProbe(reachable=True, model=MODEL, model_listed=False).status == "model missing"
    assert LLMProbe(reachable=True, model=MODEL, model_loaded=False).status == "not loaded"
    assert LLMProbe(reachable=True, model=MODEL, model_listed=True).status == "online"


class TestProbe:
    async def test_online_with_studio_details(self) -> None:
        seen: list[str] = []

        def server(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=STUDIO)

        probe = await LLMProbeCache(settings(), transport=httpx.MockTransport(server)).get()
        assert seen == ["http://localhost:8889/v1/models"]
        assert probe.status == "online"
        assert probe.context_length == 70000
        assert probe.probe_ms is not None

    @pytest.mark.parametrize(
        ("handler", "error"),
        [
            (lambda r: httpx.Response(500), "HTTP 500"),
            (lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")), "connection refused"),
            (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("x")), "timed out"),
        ],
    )
    async def test_offline(self, handler, error: str) -> None:
        probe = await LLMProbeCache(settings(), transport=httpx.MockTransport(handler)).get()
        assert probe.status == "offline"
        assert probe.error == error

    async def test_cached_between_polls(self) -> None:
        calls = 0

        def server(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=STUDIO)

        cache = LLMProbeCache(settings(), ttl_seconds=60, transport=httpx.MockTransport(server))
        await cache.get()
        await cache.get()
        assert calls == 1
        expired = LLMProbeCache(settings(), ttl_seconds=0, transport=httpx.MockTransport(server))
        await expired.get()
        await expired.get()
        assert calls == 3


def test_view_flags_a_prompt_plus_cap_beyond_the_context() -> None:
    probe = LLMProbe(reachable=True, model=MODEL, model_listed=True, context_length=8000)
    stats = {"count": 3, "avg_ms": 20500.0, "p50_ms": 18000.0, "max_prompt_tokens": 2500}
    view = llm_status_view(probe, stats, max_tokens=8192)
    assert view["context_overflow"] is True
    assert view["avg_s"] == 20.5 and view["p50_s"] == 18.0
    assert llm_status_view(probe, stats, max_tokens=4096)["context_overflow"] is False
    assert llm_status_view(probe, None, max_tokens=4096)["count"] == 0


def test_budget_and_problems() -> None:
    from src.dashboard.llm_status import llm_budget, llm_budget_problem

    probe = LLMProbe(reachable=True, model=MODEL, model_listed=True, context_length=20000)
    stats = {"tokens_per_s": 40.0, "max_prompt_tokens": 3000}
    budget = llm_budget(stats, probe, 16384)
    assert budget["min_timeout_s"] == int(16384 / 40 * 1.2) + 1  # 492 s
    assert budget["context_needed"] == 19384
    assert llm_budget_problem(16384, 600, budget) is None
    too_short = llm_budget_problem(16384, 300, budget)
    assert too_short is not None and "at least 492 s" in too_short
    overflow = llm_budget_problem(32768, 3600, llm_budget(stats, probe, 32768))
    assert overflow is not None and "20000 loaded" in overflow
    # No measurements yet → nothing to check against.
    assert llm_budget_problem(65536, 30, llm_budget(None, None, 65536)) is None
