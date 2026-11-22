"""
Purpose: Async HTTP and SSE streaming client for the OpenAI-compatible openWeightGym API.
Role: service
Patterns: service-layer, streaming-pipeline, structured-errors
Depends on: httpx, app.errors, app.schemas, app.services.thinking_parser
Exposes: StreamEvent, OpenWeightGymClient
Tests: tests/test_llm_client.py, tests/test_compaction.py
Invariants: Never hard-codes model IDs; never logs or exposes the API key; enforces X-LoadCoach-Log: off mutual exclusion.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from app.errors import UpstreamLLMError
from app.schemas import ModelInfo, ModelParticipantConfig, NarratorConfig
from app.services.thinking_parser import ThinkingParser

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Normalized streaming event emitted by OpenWeightGymClient."""

    kind: Literal["meta", "thinking", "content", "done"]
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _parse_int_header(headers: httpx.Headers, name: str) -> int | None:
    raw = headers.get(name)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _extract_response_meta(headers: httpx.Headers) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if req_id := headers.get("x-request-id"):
        meta["request_id"] = req_id
    if lc_req_id := headers.get("x-loadcoach-request-id"):
        meta["loadcoach_request_id"] = lc_req_id
    if ctx_tokens := _parse_int_header(headers, "x-loadcoach-context-tokens"):
        meta["loaded_context_tokens"] = ctx_tokens
    if answered := headers.get("x-loadcoach-answered"):
        meta["answered_model_id"] = answered
    if node := headers.get("x-loadcoach-node"):
        meta["answered_node"] = node
    if est_wait := headers.get("x-loadcoach-estimated-wait"):
        meta["estimated_wait"] = est_wait
    if ignored := headers.get("x-suite-ignored-parameters"):
        meta["ignored_parameters"] = ignored
    if allowance := headers.get("x-suite-allowance"):
        meta["allowance_warning"] = allowance
    return meta


def _build_upstream_error(
    status_code: int,
    headers: httpx.Headers,
    raw_body: bytes,
) -> UpstreamLLMError:
    err_code = "upstream_error"
    message = f"Upstream openWeightGym returned HTTP {status_code}."
    loaded_ctx: int | None = None
    max_ctx: int | None = None

    try:
        parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
        err_obj = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err_obj, dict):
            if err_obj.get("code"):
                err_code = str(err_obj["code"])
            if err_obj.get("message"):
                message = str(err_obj["message"])
            if isinstance(err_obj.get("loaded_context"), int):
                loaded_ctx = err_obj["loaded_context"]
            if isinstance(err_obj.get("max_context"), int):
                max_ctx = err_obj["max_context"]
    except (json.JSONDecodeError, ValueError):
        snippet = raw_body.decode("utf-8", errors="replace").strip()
        if snippet:
            message = snippet[:300]

    retry_after = _parse_int_header(headers, "retry-after")
    turnstile = headers.get("x-turnstile")
    request_id = headers.get("x-request-id")
    loadcoach_request_id = headers.get("x-loadcoach-request-id")

    if status_code == 401:
        err_code = err_code if err_code != "upstream_error" else "unauthorized"
    elif status_code == 403:
        err_code = err_code if err_code != "upstream_error" else "forbidden"
    elif status_code == 404:
        err_code = err_code if err_code != "upstream_error" else "model_not_found"
    elif status_code == 429:
        err_code = err_code if err_code != "upstream_error" else "rate_limited"
    elif status_code == 503 and turnstile:
        err_code = f"turnstile_{turnstile.replace('-', '_')}"

    logger.warning(
        "openWeightGym error status=%s code=%s request_id=%s lc_request_id=%s",
        status_code,
        err_code,
        request_id,
        loadcoach_request_id,
    )
    return UpstreamLLMError(
        message,
        upstream_status=status_code,
        upstream_code=err_code,
        loaded_context=loaded_ctx,
        max_context=max_ctx,
        retry_after=retry_after,
        turnstile=turnstile,
        request_id=request_id,
        loadcoach_request_id=loadcoach_request_id,
    )


class OpenWeightGymClient:
    """Async client encapsulating openWeightGym model discovery, streaming chat, and one-shot completions.

    Parameters:
        base_url: Base URL of the openWeightGym model API (e.g. `http://10.77.10.42:8442`).
        api_key: Bearer token from encrypted settings or `.env`.
        suite_application: Label sent in `X-Suite-Application`.
        default_priority: Optional `X-LoadCoach-Priority` (`high`/`normal`/`low`).
        default_sensitivity: Optional `X-LoadCoach-Sensitivity` (`normal`/`sensitive`/`restricted`).
        default_node: Optional `X-Suite-Node` destination node name.
        log_off: When True, sends `X-LoadCoach-Log: off` and omits trace/sensitivity headers.
        no_wake: When True, sends `X-Turnstile-No-Wake: 1`.
        transport: Optional httpx AsyncBaseTransport for unit tests.
    Returns:
        OpenWeightGymClient instance.
    Side effects:
        Maintains an underlying httpx.AsyncClient connection pool.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        suite_application: str = "ai-debate",
        default_priority: str | None = None,
        default_sensitivity: str | None = None,
        default_node: str | None = None,
        log_off: bool = False,
        no_wake: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.suite_application = suite_application
        self.default_priority = default_priority
        self.default_sensitivity = default_sensitivity
        self.default_node = default_node
        self.log_off = log_off
        self.no_wake = no_wake
        self._transport = transport
        self._models_cache: dict[str, ModelInfo] = {}
        self._client = self._make_client(self.base_url)

    def _make_client(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            transport=self._transport,
            timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0),
        )

    async def configure(
        self,
        *,
        base_url: str,
        api_key: str,
        suite_application: str = "ai-debate",
        default_priority: str | None = None,
        default_sensitivity: str | None = None,
        default_node: str | None = None,
        log_off: bool = False,
        no_wake: bool = False,
    ) -> None:
        """Hot-reload endpoint URL, credentials, and optional openWeightGym headers."""
        normalized = base_url.rstrip("/")
        base_changed = normalized != self.base_url
        self.base_url = normalized
        self._api_key = api_key
        self.suite_application = suite_application
        self.default_priority = default_priority
        self.default_sensitivity = default_sensitivity
        self.default_node = default_node
        self.log_off = log_off
        self.no_wake = no_wake
        if base_changed:
            old_client = self._client
            self._client = self._make_client(self.base_url)
            self._models_cache = {}
            await old_client.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    def _base_headers(self, trace_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "X-Suite-Application": self.suite_application,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self.default_priority:
            headers["X-LoadCoach-Priority"] = self.default_priority
        if self.default_node:
            headers["X-Suite-Node"] = self.default_node
        if self.no_wake:
            headers["X-Turnstile-No-Wake"] = "1"

        # Enforce app-builder.md mutual exclusion: X-LoadCoach-Log: off MUST NOT be paired
        # with X-LoadCoach-Trace or X-LoadCoach-Sensitivity.
        if self.log_off:
            headers["X-LoadCoach-Log"] = "off"
        else:
            if trace_id:
                headers["X-LoadCoach-Trace"] = trace_id
            if self.default_sensitivity:
                headers["X-LoadCoach-Sensitivity"] = self.default_sensitivity

        return headers

    async def test_connection(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        suite_application: str | None = None,
    ) -> dict[str, Any]:
        """Probe `/v1/models` against either saved or candidate Admin settings."""
        target_url = (base_url or self.base_url).rstrip("/")
        target_key = api_key if api_key is not None else self._api_key
        target_app = suite_application or self.suite_application

        temp_client = OpenWeightGymClient(
            base_url=target_url,
            api_key=target_key,
            suite_application=target_app,
            transport=self._transport,
        )
        try:
            models = await temp_client.list_chat_models()
            return {
                "ok": True,
                "endpoint": target_url,
                "chat_model_count": len(models),
                "models": [m.id for m in models],
            }
        finally:
            await temp_client.aclose()

    async def list_chat_models(self) -> list[ModelInfo]:
        """Fetch `/v1/models` and return chat-capable models with normalized openWeightGym metadata."""
        try:
            resp = await self._client.get("/v1/models", headers=self._base_headers())
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(
                f"Could not connect to openWeightGym at {self.base_url}: {exc}",
                upstream_status=503,
                upstream_code="connection_error",
            ) from exc

        if resp.status_code >= 400:
            raise _build_upstream_error(resp.status_code, resp.headers, resp.content)

        payload = resp.json()
        unavailable = set(payload.get("unavailable_machines") or [])
        models: list[ModelInfo] = []

        for entry in payload.get("data") or []:
            caps = entry.get("capabilities") or {}
            if caps.get("chat") is not True:
                continue

            model_id = str(entry.get("id", "")).strip()
            if not model_id:
                continue

            owned_by = str(entry.get("owned_by") or "loadcoach")
            context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
            locations = entry.get("locations") if isinstance(entry.get("locations"), list) else []

            loaded_context: int | None = None
            guaranteed: int | None = (
                int(context["guaranteed"]) if isinstance(context.get("guaranteed"), int) else None
            )
            ctx_max: int | None = (
                int(context["max"]) if isinstance(context.get("max"), int) else None
            )

            supports_effort = False
            effort_levels: list[str] = []
            top_caps = entry.get("chat_template_caps")
            if isinstance(top_caps, dict):
                supports_effort = bool(top_caps.get("supports_reasoning_effort"))
                effort_levels = [str(x) for x in (top_caps.get("reasoning_effort_levels") or [])]

            enabled_machines: list[str] = []
            for loc in locations:
                if not isinstance(loc, dict):
                    continue
                if loc.get("enabled", True) and loc.get("machine"):
                    enabled_machines.append(str(loc["machine"]))
                if isinstance(loc.get("loaded_context"), int):
                    loaded_context = (
                        max(loaded_context, loc["loaded_context"])
                        if loaded_context is not None
                        else loc["loaded_context"]
                    )
                loc_ctx = loc.get("context") if isinstance(loc.get("context"), dict) else {}
                if guaranteed is None and isinstance(loc_ctx.get("guaranteed"), int):
                    guaranteed = loc_ctx["guaranteed"]
                if ctx_max is None and isinstance(loc_ctx.get("max"), int):
                    ctx_max = loc_ctx["max"]
                loc_caps = loc.get("chat_template_caps")
                if isinstance(loc_caps, dict):
                    if loc_caps.get("supports_reasoning_effort"):
                        supports_effort = True
                    if loc_caps.get("reasoning_effort_levels") and not effort_levels:
                        effort_levels = [str(x) for x in loc_caps["reasoning_effort_levels"]]

            machines = [str(m) for m in (entry.get("machines") or enabled_machines)]
            node_may_wake = bool(enabled_machines) and all(m in unavailable for m in enabled_machines)

            info = ModelInfo(
                id=model_id,
                owned_by=owned_by,
                engine_id=entry.get("engine_id"),
                supports_streaming=bool(caps.get("streaming", True)),
                is_adaptor=(owned_by == "adaptor"),
                context_guaranteed=guaranteed,
                context_max=ctx_max,
                loaded_context=loaded_context,
                supports_reasoning_effort=supports_effort,
                reasoning_effort_levels=effort_levels,
                machines=machines,
                node_may_wake=node_may_wake,
            )
            models.append(info)

        self._models_cache = {m.id: m for m in models}
        return models

    def get_cached_model(self, model_id: str) -> ModelInfo | None:
        """Return cached ModelInfo if previously fetched from `/v1/models`."""
        return self._models_cache.get(model_id)

    def _build_chat_payload(
        self,
        *,
        participant: ModelParticipantConfig | NarratorConfig,
        messages: list[dict[str, str]],
        is_adaptor: bool,
        supports_streaming: bool,
        supports_reasoning_effort: bool,
    ) -> dict[str, Any]:
        if is_adaptor:
            payload: dict[str, Any] = {
                "model": participant.model_id,
                "messages": messages,
                "stream": supports_streaming,
                "n": 1,
            }
            if participant.thinking_mode == "off":
                payload["reasoning_effort"] = "none"
            elif participant.reasoning_effort:
                payload["reasoning_effort"] = participant.reasoning_effort
            return payload

        payload = {
            "model": participant.model_id,
            "messages": messages,
            "stream": supports_streaming,
            "temperature": participant.temperature,
            "top_p": participant.top_p,
            "max_tokens": participant.max_tokens,
        }
        if supports_streaming:
            payload["stream_options"] = {"include_usage": True}
        if participant.seed is not None:
            payload["seed"] = participant.seed

        if participant.thinking_mode == "off":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            if supports_reasoning_effort:
                payload["reasoning_effort"] = "none"
        elif participant.thinking_mode == "on":
            payload["chat_template_kwargs"] = {"enable_thinking": True}
            if participant.reasoning_effort and supports_reasoning_effort:
                payload["reasoning_effort"] = participant.reasoning_effort
        elif participant.reasoning_effort and supports_reasoning_effort:
            payload["reasoning_effort"] = participant.reasoning_effort

        return payload

    async def complete_summary(
        self,
        *,
        model_id: str,
        messages: list[dict[str, str]],
        max_tokens: int = 800,
        trace_id: str | None = None,
    ) -> str:
        """Run a fast non-streaming completion with thinking disabled (with 400 fallback)."""
        cached = self._models_cache.get(model_id)
        is_adaptor = cached.is_adaptor if cached else False
        headers = self._base_headers(trace_id=trace_id)

        if is_adaptor:
            payload: dict[str, Any] = {
                "model": model_id,
                "messages": messages,
                "stream": False,
                "reasoning_effort": "none",
                "n": 1,
            }
        else:
            payload = {
                "model": model_id,
                "messages": messages,
                "stream": False,
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if cached and cached.supports_reasoning_effort:
                payload["reasoning_effort"] = "none"

        try:
            resp = await self._client.post("/v1/chat/completions", headers=headers, json=payload)
            # Some engines (e.g. strict schemas) may reject chat_template_kwargs with 400; retry once without it.
            if resp.status_code == 400 and "chat_template_kwargs" in payload:
                fallback_payload = {k: v for k, v in payload.items() if k != "chat_template_kwargs"}
                resp = await self._client.post(
                    "/v1/chat/completions", headers=headers, json=fallback_payload
                )
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(
                f"Compaction request failed to reach openWeightGym: {exc}",
                upstream_status=503,
                upstream_code="connection_error",
            ) from exc

        if resp.status_code >= 400:
            raise _build_upstream_error(resp.status_code, resp.headers, resp.content)

        data = resp.json()
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        raw_content = message.get("content") or ""
        parser = ThinkingParser()
        parts = [
            ev.text
            for ev in (
                parser.feed(content_delta=str(raw_content)) + parser.flush()
            )
            if ev.kind == "content"
        ]
        return "".join(parts).strip()

    async def stream_chat_completion(
        self,
        *,
        participant: ModelParticipantConfig | NarratorConfig,
        messages: list[dict[str, str]],
        trace_id: str | None = None,
        is_adaptor: bool | None = None,
        supports_streaming: bool | None = None,
        supports_reasoning_effort: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one chat completion turn from openWeightGym, normalizing thinking and answer tokens."""
        cached = self._models_cache.get(participant.model_id)
        adaptor_flag = (
            is_adaptor if is_adaptor is not None else (cached.is_adaptor if cached else False)
        )
        stream_flag = (
            supports_streaming
            if supports_streaming is not None
            else (cached.supports_streaming if cached else True)
        )
        effort_flag = (
            supports_reasoning_effort
            if supports_reasoning_effort is not None
            else (cached.supports_reasoning_effort if cached else False)
        )

        payload = self._build_chat_payload(
            participant=participant,
            messages=messages,
            is_adaptor=adaptor_flag,
            supports_streaming=stream_flag,
            supports_reasoning_effort=effort_flag,
        )
        headers = self._base_headers(trace_id=trace_id)

        if not stream_flag:
            async for ev in self._complete_non_streaming(payload=payload, headers=headers):
                yield ev
            return

        parser = ThinkingParser()
        try:
            async with self._client.stream(
                "POST",
                "/v1/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    raw = await resp.aread()
                    raise _build_upstream_error(resp.status_code, resp.headers, raw)

                meta = _extract_response_meta(resp.headers)
                yield StreamEvent(kind="meta", meta=dict(meta))

                content_type = resp.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    raw = await resp.aread()
                    async for ev in self._emit_json_completion(raw, meta):
                        yield ev
                    return

                finish_reason: str | None = None
                prompt_tokens: int | None = None
                completion_tokens: int | None = None
                saw_done = False

                async for line in resp.aiter_lines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith(":"):
                        continue
                    if not stripped.startswith("data:"):
                        continue
                    data_str = stripped[5:].strip()
                    if data_str == "[DONE]":
                        saw_done = True
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(chunk.get("error"), dict):
                        err_obj = chunk["error"]
                        raise UpstreamLLMError(
                            str(err_obj.get("message") or "Stream terminated with upstream error."),
                            upstream_status=502,
                            upstream_code=str(err_obj.get("code") or "stream_error"),
                            request_id=meta.get("request_id"),
                            loadcoach_request_id=meta.get("loadcoach_request_id"),
                        )

                    usage = chunk.get("usage")
                    if isinstance(usage, dict):
                        if isinstance(usage.get("prompt_tokens"), int):
                            prompt_tokens = usage["prompt_tokens"]
                        if isinstance(usage.get("completion_tokens"), int):
                            completion_tokens = usage["completion_tokens"]

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])

                    delta = choice.get("delta") or {}
                    reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
                    content_piece = delta.get("content")
                    for parsed_delta in parser.feed(
                        content_delta=content_piece if isinstance(content_piece, str) else None,
                        reasoning_delta=reasoning_piece if isinstance(reasoning_piece, str) else None,
                    ):
                        yield StreamEvent(kind=parsed_delta.kind, text=parsed_delta.text)

                for parsed_delta in parser.flush():
                    yield StreamEvent(kind=parsed_delta.kind, text=parsed_delta.text)

                done_meta = dict(meta)
                done_meta["finish_reason"] = finish_reason or ("stop" if saw_done else "disconnected")
                done_meta["incomplete"] = not saw_done and finish_reason is None
                if prompt_tokens is not None:
                    done_meta["prompt_tokens"] = prompt_tokens
                if completion_tokens is not None:
                    done_meta["completion_tokens"] = completion_tokens
                yield StreamEvent(kind="done", meta=done_meta)
        except UpstreamLLMError:
            raise
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(
                f"Connection interrupted while streaming from openWeightGym: {exc}",
                upstream_status=502,
                upstream_code="stream_disconnected",
            ) from exc

    async def _complete_non_streaming(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[StreamEvent]:
        try:
            resp = await self._client.post("/v1/chat/completions", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(
                f"Could not reach openWeightGym at {self.base_url}: {exc}",
                upstream_status=503,
                upstream_code="connection_error",
            ) from exc

        if resp.status_code >= 400:
            raise _build_upstream_error(resp.status_code, resp.headers, resp.content)

        meta = _extract_response_meta(resp.headers)
        yield StreamEvent(kind="meta", meta=dict(meta))
        async for ev in self._emit_json_completion(resp.content, meta):
            yield ev

    async def _emit_json_completion(
        self,
        raw_body: bytes,
        meta: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        data = json.loads(raw_body.decode("utf-8", errors="replace"))
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}

        parser = ThinkingParser()
        reasoning_text = message.get("reasoning_content") or message.get("reasoning")
        content_text = message.get("content") or ""
        for parsed_delta in parser.feed(
            content_delta=content_text if isinstance(content_text, str) else None,
            reasoning_delta=reasoning_text if isinstance(reasoning_text, str) else None,
        ):
            yield StreamEvent(kind=parsed_delta.kind, text=parsed_delta.text)
        for parsed_delta in parser.flush():
            yield StreamEvent(kind=parsed_delta.kind, text=parsed_delta.text)

        done_meta = dict(meta)
        done_meta["finish_reason"] = choice.get("finish_reason") or "stop"
        done_meta["incomplete"] = False
        usage = data.get("usage")
        if isinstance(usage, dict):
            if isinstance(usage.get("prompt_tokens"), int):
                done_meta["prompt_tokens"] = usage["prompt_tokens"]
            if isinstance(usage.get("completion_tokens"), int):
                done_meta["completion_tokens"] = usage["completion_tokens"]
        yield StreamEvent(kind="done", meta=done_meta)
