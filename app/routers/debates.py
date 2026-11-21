"""
Purpose: FastAPI router for user-scoped debate CRUD, lifecycle controls, compaction, moderator interjections, fork, export, and SSE streaming.
Role: router
Patterns: thin-router, streaming-pipeline, structured-errors
Depends on: fastapi, json, app.routers.auth, app.schemas
Exposes: router
Tests: tests/test_debate_flow.py, tests/test_compaction.py, tests/test_auth_and_roles.py
Invariants: Enforces authenticated user ownership on every debate endpoint; never leaks another user's active debate ID or transcript.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.routers.auth import CurrentUser
from app.schemas import (
    ContinueRequest,
    DebateCreate,
    DebateDetail,
    DebateRecord,
    DebateUpdate,
    ForkRequest,
    InterjectRequest,
)

router = APIRouter(prefix="/api/debates", tags=["debates"])


@router.get("", response_model=list[DebateRecord])
async def list_debates(
    request: Request,
    user: CurrentUser,
) -> list[DebateRecord]:
    """List debates owned by the authenticated user ordered by most recently updated."""
    return request.app.state.debates_repo.list_debates(user_id=user.id)


@router.post("", response_model=DebateRecord, status_code=201)
async def create_debate(
    request: Request,
    payload: DebateCreate,
    user: CurrentUser,
) -> DebateRecord:
    """Create a new draft debate owned by the authenticated user."""
    return request.app.state.debates_repo.create_debate(payload, user_id=user.id)


@router.get("/{debate_id}", response_model=DebateDetail)
async def get_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> DebateDetail:
    """Return a single debate, its transcript messages, compactions, and any live turn snapshot."""
    repo = request.app.state.debates_repo
    manager = request.app.state.debate_manager
    debate = repo.require_debate(debate_id, user_id=user.id)
    messages = repo.list_messages(debate_id)
    compactions = repo.list_compactions(debate_id)
    return DebateDetail(
        debate=debate,
        messages=messages,
        compactions=compactions,
        active_turn=manager.get_active_turn(debate_id),
        global_active_debate_id=manager.get_active_debate_id_for_user(user.id),
    )


@router.patch("/{debate_id}", response_model=DebateRecord)
async def update_debate(
    request: Request,
    debate_id: str,
    patch: DebateUpdate,
    user: CurrentUser,
) -> DebateRecord:
    """Update debate settings or participant configurations."""
    return request.app.state.debates_repo.update_debate(debate_id, patch, user_id=user.id)


@router.delete("/{debate_id}", status_code=204)
async def delete_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> None:
    """Stop (if running) and delete a debate owned by the current user."""
    repo = request.app.state.debates_repo
    repo.require_debate(debate_id, user_id=user.id)
    manager = request.app.state.debate_manager
    if manager.active_debate_id == debate_id:
        await manager.stop_debate(debate_id, user_id=user.id)
    repo.delete_debate(debate_id, user_id=user.id)


@router.post("/{debate_id}/start", response_model=DebateRecord)
async def start_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> DebateRecord:
    """Start a draft or paused debate."""
    return await request.app.state.debate_manager.start_debate(debate_id, user_id=user.id)


@router.post("/{debate_id}/pause", response_model=DebateRecord)
async def pause_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> DebateRecord:
    """Pause the running debate."""
    return await request.app.state.debate_manager.pause_debate(debate_id, user_id=user.id)


@router.post("/{debate_id}/resume", response_model=DebateRecord)
async def resume_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> DebateRecord:
    """Resume a paused or errored debate."""
    return await request.app.state.debate_manager.resume_debate(debate_id, user_id=user.id)


@router.post("/{debate_id}/stop", response_model=DebateRecord)
async def stop_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> DebateRecord:
    """Stop the debate."""
    return await request.app.state.debate_manager.stop_debate(debate_id, user_id=user.id)


@router.post("/{debate_id}/continue", response_model=DebateRecord)
async def continue_debate(
    request: Request,
    debate_id: str,
    payload: ContinueRequest,
    user: CurrentUser,
) -> DebateRecord:
    """Add turns or time to a debate that hit its limit and resume running."""
    return await request.app.state.debate_manager.continue_debate(
        debate_id, payload, user_id=user.id
    )


@router.post("/{debate_id}/compact")
async def compact_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> dict[str, Any]:
    """Trigger manual context compaction immediately (when idle) or queue for next turn (when running)."""
    queued, compaction = await request.app.state.debate_manager.compact_now(
        debate_id, user_id=user.id
    )
    return {
        "queued": queued,
        "compaction": compaction.model_dump() if compaction else None,
    }


@router.post("/{debate_id}/interject")
async def interject(
    request: Request,
    debate_id: str,
    payload: InterjectRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    """Inject a moderator message immediately (if idle) or queue for the next turn (if running)."""
    msg = await request.app.state.debate_manager.interject(
        debate_id, payload.content, user_id=user.id
    )
    if msg is None:
        return {"queued": True, "message": None}
    return {"queued": False, "message": msg.model_dump()}


@router.post("/{debate_id}/fork", response_model=DebateRecord, status_code=201)
async def fork_debate(
    request: Request,
    debate_id: str,
    payload: ForkRequest,
    user: CurrentUser,
) -> DebateRecord:
    """Duplicate a debate's configuration into a new draft debate with optional side swap."""
    return request.app.state.debates_repo.fork_debate(
        debate_id,
        swap_sides=payload.swap_sides,
        title=payload.title,
        user_id=user.id,
    )


@router.get("/{debate_id}/export")
async def export_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
    fmt: Literal["md", "json"] = Query(default="md", alias="format"),
    include_thinking: bool = Query(default=True),
    include_stats: bool = Query(default=True),
) -> Any:
    """Export a debate transcript as Markdown or JSON."""
    repo = request.app.state.debates_repo
    debate = repo.require_debate(debate_id, user_id=user.id)
    messages = repo.list_messages(debate_id)
    compactions = repo.list_compactions(debate_id)

    if fmt == "json":
        serialized_msgs = []
        for msg in messages:
            item = msg.model_dump()
            if not include_thinking:
                item.pop("thinking", None)
            if not include_stats:
                for k in (
                    "prompt_tokens",
                    "completion_tokens",
                    "loaded_context_tokens",
                    "duration_ms",
                    "tokens_per_sec",
                    "request_id",
                    "loadcoach_request_id",
                ):
                    item.pop(k, None)
            serialized_msgs.append(item)
        return JSONResponse(
            content={
                "debate": debate.model_dump(),
                "messages": serialized_msgs,
                "compactions": [c.model_dump() for c in compactions],
            },
            headers={
                "Content-Disposition": f'attachment; filename="{debate.id}.json"',
            },
        )

    compactions_by_msg: dict[str, list[Any]] = {}
    for c in compactions:
        compactions_by_msg.setdefault(c.through_message_id, []).append(c)

    lines: list[str] = [
        f"# {debate.title}",
        "",
        f"- **Model A**: {debate.model_a_config.effective_label('Model A')} (`{debate.model_a_config.model_id}`)",
        f"- **Model B**: {debate.model_b_config.effective_label('Model B')} (`{debate.model_b_config.model_id}`)",
        f"- **Status**: {debate.status}",
    ]
    if debate.scene_prompt.strip():
        lines.extend(["", "## Scene / Topic", "", debate.scene_prompt.strip()])
    lines.extend(["", "## Transcript", ""])

    for msg in messages:
        if msg.sender == "A":
            who = debate.model_a_config.effective_label("Model A")
        elif msg.sender == "B":
            who = debate.model_b_config.effective_label("Model B")
        else:
            who = "Moderator"
        lines.append(f"### Turn {msg.turn_index} — {who}")
        if include_stats and msg.sender in ("A", "B"):
            stat_parts = []
            if msg.answered_model_id or msg.model_id:
                stat_parts.append(f"model: `{msg.answered_model_id or msg.model_id}`")
            if msg.completion_tokens is not None:
                stat_parts.append(f"tokens: {msg.completion_tokens}")
            if msg.tokens_per_sec is not None:
                stat_parts.append(f"{msg.tokens_per_sec} tok/s")
            if msg.duration_ms is not None:
                stat_parts.append(f"{msg.duration_ms / 1000:.1f}s")
            if stat_parts:
                lines.append(f"*{' · '.join(stat_parts)}*")
        lines.append("")
        if include_thinking and msg.thinking.strip():
            lines.extend(["<details><summary>Thinking</summary>", "", msg.thinking.strip(), "", "</details>", ""])
        lines.extend([msg.content.strip(), ""])

        for comp in compactions_by_msg.get(msg.id, []):
            lines.extend(
                [
                    f"> **Context Compacted** ({comp.trigger_reason}, by `{comp.summarizer_model_id}`)",
                    ">",
                    f"> {comp.summary_text}",
                    "",
                ]
            )

    return PlainTextResponse(
        content="\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{debate.id}.md"'},
    )


@router.get("/{debate_id}/stream")
async def stream_debate(
    request: Request,
    debate_id: str,
    user: CurrentUser,
) -> StreamingResponse:
    """Open a live Server-Sent Events stream for a debate owned by the authenticated user."""
    request.app.state.debates_repo.require_debate(debate_id, user_id=user.id)
    manager = request.app.state.debate_manager

    async def event_generator() -> AsyncIterator[str]:
        async for ev in manager.subscribe(debate_id, user_id=user.id):
            if await request.is_disconnected():
                break
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
