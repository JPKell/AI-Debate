"""
Purpose: Unit tests for OpenWeightGymClient model discovery, request payload/headers, SSE streaming, and error mapping.
Role: test
Patterns: contract-first-test, service-layer, structured-errors
Depends on: app.services.llm_client, app.errors, app.schemas, httpx
Exposes: test_list_chat_models_filters_and_parses_metadata, test_stream_chat_completion_headers_and_thinking, test_adaptor_model_filters_sampling_fields, test_context_too_small_error_parsing, test_log_off_mutual_exclusion_headers, test_turnstile_503_and_retry_after_error_parsing
Tests: tests/test_llm_client.py
Invariants: All HTTP interactions are mocked via httpx.MockTransport; no live network calls occur.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.errors import UpstreamLLMError
from app.schemas import ModelParticipantConfig
from app.services.llm_client import OpenWeightGymClient


@pytest.mark.asyncio
async def test_list_chat_models_filters_and_parses_metadata() -> None:
    payload = {
        "object": "list",
        "available_machines": ["gpu-1"],
        "unavailable_machines": ["gpu-2"],
        "data": [
            {
                "id": "embed-small",
                "owned_by": "loadcoach",
                "capabilities": {"chat": False, "embeddings": True},
                "embedding": True,
            },
            {
                "id": "qwen-chat",
                "owned_by": "loadcoach",
                "capabilities": {"chat": True, "streaming": True},
                "context": {"guaranteed": 8192, "max": 32768},
                "locations": [
                    {
                        "machine": "gpu-1",
                        "enabled": True,
                        "loaded_context": 20480,
                        "chat_template_caps": {
                            "supports_reasoning_effort": True,
                            "reasoning_effort_levels": ["low", "medium", "high"],
                        },
                    }
                ],
            },
            {
                "id": "asleep-chat",
                "owned_by": "adaptor",
                "capabilities": {"chat": True, "streaming": False},
                "locations": [{"machine": "gpu-2", "enabled": True}],
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["X-Suite-Application"] == "ai-debate"
        return httpx.Response(200, json=payload)

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        suite_application="ai-debate",
        transport=httpx.MockTransport(handler),
    )
    models = await client.list_chat_models()
    await client.aclose()

    assert [m.id for m in models] == ["qwen-chat", "asleep-chat"]
    assert models[0].context_guaranteed == 8192
    assert models[0].context_max == 32768
    assert models[0].loaded_context == 20480
    assert models[0].supports_reasoning_effort is True
    assert models[0].reasoning_effort_levels == ["low", "medium", "high"]
    assert models[0].node_may_wake is False
    assert models[1].is_adaptor is True
    assert models[1].supports_streaming is False
    assert models[1].node_may_wake is True


@pytest.mark.asyncio
async def test_stream_chat_completion_headers_and_thinking() -> None:
    captured: dict[str, object] = {}

    sse_body = (
        'data: {"choices":[{"delta":{"reasoning_content":"Plan..."},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Hello "},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{"content":"there!"},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":42,"completion_tokens":12}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Request-ID": "req-123",
                "X-LoadCoach-Request-ID": "lc-456",
                "X-LoadCoach-Context-Tokens": "16384",
                "X-LoadCoach-Answered": "qwen-chat",
                "X-LoadCoach-Node": "gpu-1",
            },
            content=sse_body.encode(),
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="secret-key",
        suite_application="ai-debate",
        transport=httpx.MockTransport(handler),
    )
    participant = ModelParticipantConfig(
        model_id="qwen-chat",
        thinking_mode="on",
        temperature=0.7,
        max_tokens=1024,
    )
    events = [
        ev
        async for ev in client.stream_chat_completion(
            participant=participant,
            messages=[{"role": "user", "content": "Hi"}],
            trace_id="debate-abc",
        )
    ]
    await client.aclose()

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["stream"] is True
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-loadcoach-trace"] == "debate-abc"

    thinking = "".join(e.text for e in events if e.kind == "thinking")
    content = "".join(e.text for e in events if e.kind == "content")
    done_events = [e for e in events if e.kind == "done"]
    assert thinking == "Plan..."
    assert content == "Hello there!"
    assert len(done_events) == 1
    assert done_events[0].meta["loaded_context_tokens"] == 16384
    assert done_events[0].meta["prompt_tokens"] == 42
    assert done_events[0].meta["completion_tokens"] == 12
    assert done_events[0].meta["request_id"] == "req-123"


@pytest.mark.asyncio
async def test_adaptor_model_filters_sampling_fields() -> None:
    captured_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Adaptor reply"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="secret-key",
        transport=httpx.MockTransport(handler),
    )
    participant = ModelParticipantConfig(
        model_id="agent-x",
        temperature=0.9,
        top_p=0.8,
        max_tokens=512,
        thinking_mode="off",
    )
    events = [
        ev
        async for ev in client.stream_chat_completion(
            participant=participant,
            messages=[{"role": "user", "content": "Go"}],
            is_adaptor=True,
            supports_streaming=False,
        )
    ]
    await client.aclose()

    assert set(captured_body.keys()) <= {"model", "messages", "stream", "reasoning_effort", "n"}
    assert captured_body["stream"] is False
    assert any(e.kind == "content" and e.text == "Adaptor reply" for e in events)


@pytest.mark.asyncio
async def test_context_too_small_error_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"X-Request-ID": "req-err"},
            json={
                "error": {
                    "code": "context_too_small",
                    "type": "exceed_context_size_error",
                    "loaded_context": 8192,
                    "max_context": 32768,
                    "message": "Prompt exceeds loaded window.",
                }
            },
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="secret-key",
        transport=httpx.MockTransport(handler),
    )
    participant = ModelParticipantConfig(model_id="qwen-chat")

    with pytest.raises(UpstreamLLMError) as exc_info:
        async for _ in client.stream_chat_completion(
            participant=participant,
            messages=[{"role": "user", "content": "Too long"}],
        ):
            pass

    await client.aclose()
    err = exc_info.value
    assert err.upstream_status == 400
    assert err.upstream_code == "context_too_small"
    assert err.loaded_context == 8192
    assert err.max_context == 32768
    assert err.request_id == "req-err"


@pytest.mark.asyncio
async def test_log_off_mutual_exclusion_headers() -> None:
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="secret-key",
        suite_application="ai-debate",
        default_priority="high",
        default_sensitivity="sensitive",
        default_node="gpu-1",
        log_off=True,
        no_wake=True,
        transport=httpx.MockTransport(handler),
    )
    participant = ModelParticipantConfig(model_id="qwen-chat")
    events = [
        ev
        async for ev in client.stream_chat_completion(
            participant=participant,
            messages=[{"role": "user", "content": "Hi"}],
            trace_id="debate-should-be-omitted",
            supports_streaming=False,
        )
    ]
    await client.aclose()

    assert any(e.kind == "content" for e in events)
    assert captured_headers["x-loadcoach-log"] == "off"
    assert captured_headers["x-loadcoach-priority"] == "high"
    assert captured_headers["x-suite-node"] == "gpu-1"
    assert captured_headers["x-turnstile-no-wake"] == "1"
    # Mutual exclusion: trace and sensitivity MUST be omitted when X-LoadCoach-Log: off is set
    assert "x-loadcoach-trace" not in captured_headers
    assert "x-loadcoach-sensitivity" not in captured_headers


@pytest.mark.asyncio
async def test_turnstile_503_and_retry_after_error_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={
                "X-Turnstile": "compute-off",
                "Retry-After": "12",
                "X-Request-ID": "req-503",
            },
            json={"error": {"message": "Compute node is asleep.", "code": "node_unavailable"}},
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="secret-key",
        transport=httpx.MockTransport(handler),
    )
    participant = ModelParticipantConfig(model_id="qwen-chat")

    with pytest.raises(UpstreamLLMError) as exc_info:
        async for _ in client.stream_chat_completion(
            participant=participant,
            messages=[{"role": "user", "content": "Wake up"}],
        ):
            pass

    await client.aclose()
    err = exc_info.value
    assert err.upstream_status == 503
    assert err.turnstile == "compute-off"
    assert err.retry_after == 12
    assert err.request_id == "req-503"


