"""
Purpose: End-to-end tests for DebateManager orchestration, global single-debate lock, moderator interjection, fork, and export.
Role: test
Patterns: contract-first-test, service-layer, repository-pattern, streaming-pipeline
Depends on: app.repositories.db, app.repositories.debates_repo, app.services.llm_client, app.services.debate_manager, app.schemas, httpx
Exposes: test_end_to_end_two_model_debate_and_limits, test_global_single_debate_lock_conflict, test_fork_swap_sides_and_moderator_interjection, test_pause_resume_continue_and_mid_debate_reconnect_snapshot, test_cross_user_409_redaction_and_export_filters, test_upstream_404_and_503_turnstile_handling
Tests: tests/test_debate_flow.py
Invariants: Uses isolated temporary SQLite databases and mocked openWeightGym SSE responses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.errors import DebateBusyError
from app.repositories.db import init_db
from app.repositories.debates_repo import DebatesRepository
from app.schemas import DebateCreate, ModelParticipantConfig
from app.services.debate_manager import DebateManager
from app.services.llm_client import OpenWeightGymClient


def _sse_response(model_name: str, reply: str, thinking: str = "") -> httpx.Response:
    chunks: list[str] = []
    if thinking:
        chunks.append(
            f'data: {json.dumps({"choices": [{"delta": {"reasoning_content": thinking}, "finish_reason": None}]})}\n\n'
        )
    chunks.append(
        f'data: {json.dumps({"choices": [{"delta": {"content": reply}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 15}})}\n\n'
    )
    chunks.append("data: [DONE]\n\n")
    return httpx.Response(
        200,
        headers={
            "Content-Type": "text/event-stream",
            "X-Request-ID": f"req-{model_name}",
            "X-LoadCoach-Context-Tokens": "16384",
            "X-LoadCoach-Answered": model_name,
            "X-LoadCoach-Node": "mock-gpu",
        },
        content="".join(chunks).encode(),
    )


@pytest.mark.asyncio
async def test_end_to_end_two_model_debate_and_limits(tmp_path: Path) -> None:
    db_path = tmp_path / "debate.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)

    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "model-alpha", "owned_by": "loadcoach", "capabilities": {"chat": True, "streaming": True}},
                        {"id": "model-beta", "owned_by": "loadcoach", "capabilities": {"chat": True, "streaming": True}},
                    ],
                },
            )
        body = json.loads(request.content.decode())
        model = body["model"]
        seen_models.append(model)
        return _sse_response(
            model,
            reply=f"Argument from {model} (turn {len(seen_models)}).",
            thinking=f"Private reasoning by {model}",
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    manager = DebateManager(repo=repo, llm_client=client)

    debate = repo.create_debate(
        DebateCreate(
            title="AI Governance",
            scene_prompt="Should frontier AI weights be open?",
            first_speaker="A",
            turn_limit=4,
            model_a_config=ModelParticipantConfig(model_id="model-alpha", label="Open Advocate"),
            model_b_config=ModelParticipantConfig(model_id="model-beta", label="Safety Hawk"),
        )
    )

    await manager.start_debate(debate.id)
    await manager.wait_for_idle(timeout=5.0)
    await client.aclose()

    updated = repo.get_debate(debate.id)
    assert updated is not None
    assert updated.status == "limit_reached"
    assert updated.model_a_ctx_loaded == 16384
    assert updated.model_b_ctx_loaded == 16384
    assert seen_models == ["model-alpha", "model-beta", "model-alpha", "model-beta"]

    msgs = repo.list_messages(debate.id)
    assert len(msgs) == 4
    assert [m.sender for m in msgs] == ["A", "B", "A", "B"]
    assert msgs[0].thinking == "Private reasoning by model-alpha"
    assert msgs[0].answered_node == "mock-gpu"


@pytest.mark.asyncio
async def test_global_single_debate_lock_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "lock.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)
    release_event = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        await release_event.wait()
        body = json.loads(request.content.decode())
        return _sse_response(body["model"], reply="Done")

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    manager = DebateManager(repo=repo, llm_client=client)

    d1 = repo.create_debate(
        DebateCreate(
            title="Debate 1",
            scene_prompt="Topic 1",
            turn_limit=1,
            model_a_config=ModelParticipantConfig(model_id="m-a"),
            model_b_config=ModelParticipantConfig(model_id="m-b"),
        )
    )
    d2 = repo.create_debate(
        DebateCreate(
            title="Debate 2",
            scene_prompt="Topic 2",
            turn_limit=1,
            model_a_config=ModelParticipantConfig(model_id="m-a"),
            model_b_config=ModelParticipantConfig(model_id="m-b"),
        )
    )

    await manager.start_debate(d1.id)
    with pytest.raises(DebateBusyError):
        await manager.start_debate(d2.id)

    release_event.set()
    await manager.wait_for_idle(timeout=5.0)
    await client.aclose()


@pytest.mark.asyncio
async def test_fork_swap_sides_and_moderator_interjection(tmp_path: Path) -> None:
    db_path = tmp_path / "fork.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)
    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": []})),
    )
    manager = DebateManager(repo=repo, llm_client=client)

    orig = repo.create_debate(
        DebateCreate(
            title="Original",
            scene_prompt="Cats vs Dogs",
            model_a_config=ModelParticipantConfig(
                model_id="m-a",
                side="Cats win",
                seed=42,
                system_prompt_override="Custom override A",
            ),
            model_b_config=ModelParticipantConfig(model_id="m-b", side="Dogs win", seed=99),
        )
    )
    mod_msg = await manager.interject(orig.id, "Consider apartments.")
    assert mod_msg is not None
    assert mod_msg.sender == "moderator"

    copied = repo.fork_debate(orig.id, swap_sides=False)
    assert copied.id != orig.id
    assert copied.status == "draft"
    assert copied.model_a_config.side == "Cats win"
    assert copied.model_b_config.side == "Dogs win"
    assert copied.model_a_config.seed == 42
    assert copied.model_a_config.system_prompt_override == "Custom override A"

    forked = repo.fork_debate(orig.id, swap_sides=True)
    assert forked.id != orig.id
    assert forked.status == "draft"
    assert forked.model_a_config.side == "Dogs win"
    assert forked.model_b_config.side == "Cats win"
    assert forked.model_a_config.seed == 42
    await client.aclose()


@pytest.mark.asyncio
async def test_pause_resume_continue_and_mid_debate_reconnect_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "reconnect.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)

    turn_started = asyncio.Event()
    allow_finish = asyncio.Event()

    async def stream_gen():
        yield (
            f'data: {json.dumps({"choices": [{"delta": {"reasoning_content": "Halfway thought"}, "finish_reason": None}]})}\n\n'
        ).encode()
        yield (
            f'data: {json.dumps({"choices": [{"delta": {"content": "Partial answer "}, "finish_reason": None}]})}\n\n'
        ).encode()
        turn_started.set()
        await allow_finish.wait()
        yield (
            f'data: {json.dumps({"choices": [{"delta": {"content": "complete."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 30, "completion_tokens": 10}})}\n\n'
            "data: [DONE]\n\n"
        ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        if not turn_started.is_set():
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=httpx.AsyncByteStream.__subclasses__()[0]() if False else _AsyncIterStream(stream_gen()),
            )
        body = json.loads(request.content.decode())
        return _sse_response(body["model"], reply="Continued reply.")

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    manager = DebateManager(repo=repo, llm_client=client)

    debate = repo.create_debate(
        DebateCreate(
            title="Reconnect Test",
            scene_prompt="Topic",
            turn_limit=1,
            model_a_config=ModelParticipantConfig(model_id="m-a"),
            model_b_config=ModelParticipantConfig(model_id="m-b"),
        )
    )

    await manager.start_debate(debate.id)
    await asyncio.wait_for(turn_started.wait(), timeout=3.0)
    await asyncio.sleep(0.05)

    # Mid-debate reconnect snapshot captures buffered thinking & partial content
    snap = manager.get_active_turn(debate.id)
    assert snap is not None
    assert snap.thinking == "Halfway thought"
    assert snap.content == "Partial answer "

    allow_finish.set()
    await manager.wait_for_idle(timeout=5.0)
    assert repo.require_debate(debate.id).status == "limit_reached"

    # Continue by +1 turn
    from app.schemas import ContinueRequest

    await manager.continue_debate(debate.id, ContinueRequest(add_turns=1))
    await manager.wait_for_idle(timeout=5.0)
    assert repo.require_debate(debate.id).status == "limit_reached"
    assert len(repo.list_messages(debate.id)) == 2
    await client.aclose()


class _AsyncIterStream(httpx.AsyncByteStream):
    def __init__(self, aiter):
        self._aiter = aiter

    async def __aiter__(self):
        async for chunk in self._aiter:
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_cross_user_409_redaction_and_export_filters(tmp_path: Path) -> None:
    from app.config import AppConfig
    from app.main import create_app

    cfg = AppConfig(
        owg_protocol="http",
        owg_host="mock-owg",
        owg_port=8442,
        owg_api_key="secret",
        data_dir=tmp_path,
    )
    app = create_app(cfg)
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await release.wait()
        body = json.loads(request.content.decode())
        return _sse_response(body["model"], reply="Reply", thinking="Private chain")

    async with app.router.lifespan_context(app):
        # Swap in mock transport
        await app.state.llm_client.aclose()
        app.state.llm_client = OpenWeightGymClient(
            base_url="http://mock-owg:8442",
            api_key="secret",
            transport=httpx.MockTransport(handler),
        )
        app.state.debate_manager._llm_client = app.state.llm_client

        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as c_alice,
            httpx.AsyncClient(transport=transport, base_url="http://test") as c_bob,
        ):
            await c_alice.post("/api/auth/register", json={"username": "alice", "password": "password123"})
            await c_bob.post("/api/auth/register", json={"username": "bob", "password": "password123"})

            d_alice = (
                await c_alice.post(
                    "/api/debates",
                    json={
                        "title": "Alice Running",
                        "turn_limit": 1,
                        "model_a_config": {"model_id": "m-a"},
                        "model_b_config": {"model_id": "m-b"},
                    },
                )
            ).json()["id"]
            d_alice_2 = (
                await c_alice.post(
                    "/api/debates",
                    json={
                        "title": "Alice Second",
                        "turn_limit": 1,
                        "model_a_config": {"model_id": "m-a"},
                        "model_b_config": {"model_id": "m-b"},
                    },
                )
            ).json()["id"]
            d_bob = (
                await c_bob.post(
                    "/api/debates",
                    json={
                        "title": "Bob Debate",
                        "turn_limit": 1,
                        "model_a_config": {"model_id": "m-a"},
                        "model_b_config": {"model_id": "m-b"},
                    },
                )
            ).json()["id"]

            assert (await c_alice.post(f"/api/debates/{d_alice}/start")).status_code == 200

            # Same user gets 409 WITH active_debate_id for jump link
            r_same = await c_alice.post(f"/api/debates/{d_alice_2}/start")
            assert r_same.status_code == 409
            assert r_same.json()["error"]["details"]["active_debate_id"] == d_alice

            # Different user gets 409 WITHOUT active_debate_id (no leakage)
            r_other = await c_bob.post(f"/api/debates/{d_bob}/start")
            assert r_other.status_code == 409
            assert "active_debate_id" not in r_other.json()["error"].get("details", {})

            release.set()
            await app.state.debate_manager.wait_for_idle(timeout=5.0)

            # Export JSON with include_thinking=false & include_stats=false
            r_json = await c_alice.get(
                f"/api/debates/{d_alice}/export?format=json&include_thinking=false&include_stats=false"
            )
            assert r_json.status_code == 200
            exported_msg = r_json.json()["messages"][0]
            assert "thinking" not in exported_msg
            assert "prompt_tokens" not in exported_msg

            # Export MD with include_thinking=false
            r_md = await c_alice.get(
                f"/api/debates/{d_alice}/export?format=md&include_thinking=false&include_stats=false"
            )
            assert r_md.status_code == 200
            assert "Private chain" not in r_md.text


@pytest.mark.asyncio
async def test_upstream_404_and_503_turnstile_handling(tmp_path: Path) -> None:
    db_path = tmp_path / "errors.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)
    refreshed_models = {"count": 0}

    def handler_404(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            refreshed_models["count"] += 1
            return httpx.Response(200, json={"object": "list", "data": []})
        return httpx.Response(
            404,
            json={"error": {"code": "model_not_found", "message": "Model missing."}},
        )

    client_404 = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(handler_404),
    )
    mgr_404 = DebateManager(repo=repo, llm_client=client_404)
    d1 = repo.create_debate(
        DebateCreate(
            title="404 Test",
            model_a_config=ModelParticipantConfig(model_id="gone-model"),
            model_b_config=ModelParticipantConfig(model_id="m-b"),
        )
    )
    await mgr_404.start_debate(d1.id)
    await mgr_404.wait_for_idle(timeout=5.0)
    await client_404.aclose()
    assert refreshed_models["count"] == 1
    assert repo.require_debate(d1.id).status == "paused"

    def handler_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={"X-Turnstile": "compute-off", "Retry-After": "15"},
            json={"error": {"message": "Node offline."}},
        )

    client_503 = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(handler_503),
    )
    mgr_503 = DebateManager(repo=repo, llm_client=client_503)
    d2 = repo.create_debate(
        DebateCreate(
            title="503 Test",
            model_a_config=ModelParticipantConfig(model_id="m-a"),
            model_b_config=ModelParticipantConfig(model_id="m-b"),
        )
    )
    await mgr_503.start_debate(d2.id)
    await mgr_503.wait_for_idle(timeout=5.0)
    await client_503.aclose()
    d2_after = repo.require_debate(d2.id)
    assert d2_after.status == "paused"
    assert d2_after.last_error is not None
    assert "15s" in d2_after.last_error
    assert "compute-off" in d2_after.last_error

