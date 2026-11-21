"""
Purpose: Unit and integration tests for context window tracking, automatic/manual compaction, and 400 context_too_small recovery.
Role: test
Patterns: contract-first-test, service-layer
Depends on: app.repositories.db, app.repositories.debates_repo, app.services.llm_client, app.services.compaction, app.services.debate_manager, app.schemas, httpx
Exposes: test_threshold_detection_with_asymmetric_context_limits, test_manual_compaction_preserves_messages_and_disables_thinking, test_reactive_context_too_small_recovery_retries_once
Tests: tests/test_compaction.py
Invariants: Uses isolated temporary SQLite databases and mocked openWeightGym HTTP responses.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.repositories.db import init_db
from app.repositories.debates_repo import DebatesRepository
from app.schemas import DebateCreate, ModelParticipantConfig
from app.services.compaction import CompactionService
from app.services.debate_manager import DebateManager
from app.services.llm_client import OpenWeightGymClient
from app.services.prompt_builder import build_messages_for_speaker


def _sse_reply(text: str, *, prompt_tokens: int = 100, completion_tokens: int = 25, ctx_tokens: int = 8192) -> httpx.Response:
    body = (
        f'data: {json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})}\n\n'
        "data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        headers={
            "Content-Type": "text/event-stream",
            "X-LoadCoach-Context-Tokens": str(ctx_tokens),
        },
        content=body.encode(),
    )


@pytest.mark.asyncio
async def test_threshold_detection_with_asymmetric_context_limits(tmp_path: Path) -> None:
    db_path = tmp_path / "compact_thresh.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)

    debate = repo.create_debate(
        DebateCreate(
            title="Asymmetric Windows",
            scene_prompt="Discuss distributed consensus.",
            auto_compact_threshold=0.75,
            keep_recent_turns=2,
            model_a_config=ModelParticipantConfig(model_id="small-ctx", max_tokens=1000),
            model_b_config=ModelParticipantConfig(model_id="large-ctx", max_tokens=1000),
        )
    )
    repo.update_model_context(debate.id, "A", loaded=4000, guaranteed=4000, max_ctx=8192)
    repo.update_model_context(debate.id, "B", loaded=32000, guaranteed=16000, max_ctx=32000)

    for i in range(6):
        repo.add_message(
            debate.id,
            sender="A" if i % 2 == 0 else "B",
            model_id="small-ctx" if i % 2 == 0 else "large-ctx",
            content=f"Turn {i + 1}: " + ("consensus argument " * 60),
            prompt_tokens=2500 if i % 2 == 0 else 2500,
            completion_tokens=100,
            loaded_context_tokens=4000 if i % 2 == 0 else 32000,
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": []})),
    )
    svc = CompactionService(repo=repo, llm_client=client)
    updated = repo.require_debate(debate.id)
    history = repo.list_messages(debate.id)

    # Model A (4000 ctx): 2500 + new_msg + 1000 > 0.75 * 4000 (3000) -> True
    assert svc.should_auto_compact(updated, history, None, "A") is True
    # Model B (32000 ctx): well below 0.75 * 32000 (24000) -> False
    assert svc.should_auto_compact(updated, history, None, "B") is False
    await client.aclose()


@pytest.mark.asyncio
async def test_manual_compaction_preserves_messages_and_disables_thinking(tmp_path: Path) -> None:
    db_path = tmp_path / "compact_manual.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)

    captured_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured_payloads.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Neutral summary of turns 1-4."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 40},
            },
        )

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    svc = CompactionService(repo=repo, llm_client=client)

    debate = repo.create_debate(
        DebateCreate(
            title="Compaction Test",
            scene_prompt="Debate space colonization.",
            keep_recent_turns=2,
            model_a_config=ModelParticipantConfig(model_id="model-small"),
            model_b_config=ModelParticipantConfig(model_id="model-large"),
        )
    )
    repo.update_model_context(debate.id, "A", loaded=8192)
    repo.update_model_context(debate.id, "B", loaded=32768)

    msgs = []
    for i in range(6):
        msgs.append(
            repo.add_message(
                debate.id,
                sender="A" if i % 2 == 0 else "B",
                model_id="model-small" if i % 2 == 0 else "model-large",
                content=f"Substantive point number {i + 1}.",
            )
        )

    compaction = await svc.compact_debate(debate.id, trigger_reason="manual")
    await client.aclose()

    assert compaction is not None
    assert compaction.trigger_reason == "manual"
    assert compaction.through_message_id == msgs[3].id
    assert compaction.summarizer_model_id == "model-large"
    assert "Neutral summary" in compaction.summary_text

    # Thinking must be explicitly disabled on summarization call
    assert captured_payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}

    # Original messages must NOT be deleted
    all_msgs = repo.list_messages(debate.id)
    assert len(all_msgs) == 6

    # Subsequent prompt builder sends summary + only messages AFTER through_message_id
    prompt_msgs = build_messages_for_speaker(
        debate=repo.require_debate(debate.id),
        history=all_msgs,
        speaker="A",
        latest_compaction=compaction,
    )
    assert "Neutral summary of turns 1-4." in prompt_msgs[0]["content"]
    joined_turns = " ".join(m["content"] for m in prompt_msgs[1:])
    assert "Substantive point number 1." not in joined_turns
    assert "Substantive point number 5." in joined_turns
    assert "Substantive point number 6." in joined_turns


@pytest.mark.asyncio
async def test_reactive_context_too_small_recovery_retries_once(tmp_path: Path) -> None:
    db_path = tmp_path / "compact_reactive.db"
    init_db(db_path)
    repo = DebatesRepository(db_path)

    call_count = {"chat": 0, "summary": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("stream") is False:
            call_count["summary"] += 1
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Compacted prior context."}, "finish_reason": "stop"}],
                },
            )
        call_count["chat"] += 1
        if call_count["chat"] == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_too_small",
                        "loaded_context": 4096,
                        "max_context": 32768,
                        "message": "Prompt exceeds loaded window.",
                    }
                },
            )
        return _sse_reply("Recovered reply after compaction!", ctx_tokens=4096)

    client = OpenWeightGymClient(
        base_url="http://mock-owg:8442",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    compaction_svc = CompactionService(repo=repo, llm_client=client)
    manager = DebateManager(repo=repo, llm_client=client, compaction_service=compaction_svc)

    debate = repo.create_debate(
        DebateCreate(
            title="Reactive Recovery",
            scene_prompt="Long debate",
            turn_limit=7,
            keep_recent_turns=2,
            model_a_config=ModelParticipantConfig(model_id="m-a"),
            model_b_config=ModelParticipantConfig(model_id="m-b"),
        )
    )
    for i in range(6):
        repo.add_message(
            debate.id,
            sender="A" if i % 2 == 0 else "B",
            model_id="m-a" if i % 2 == 0 else "m-b",
            content=f"Earlier message {i + 1}",
        )

    await manager.start_debate(debate.id)
    await manager.wait_for_idle(timeout=5.0)
    await client.aclose()

    updated = repo.require_debate(debate.id)
    assert updated.status == "limit_reached"
    assert updated.model_a_ctx_loaded == 4096
    assert call_count["summary"] == 1
    assert call_count["chat"] == 2

    compactions = repo.list_compactions(debate.id)
    assert len(compactions) == 1
    assert compactions[0].trigger_reason == "context_too_small"
    msgs = repo.list_messages(debate.id)
    assert msgs[-1].content == "Recovered reply after compaction!"
