"""
Purpose: Orchestrate live two-model debates with a global single-debate lock, background streaming, context compaction, and SSE pub/sub.
Role: service
Patterns: service-layer, streaming-pipeline, structured-errors
Depends on: asyncio, contextlib, random, time, app.errors, app.repositories.debates_repo, app.schemas, app.services.compaction, app.services.llm_client, app.services.prompt_builder
Exposes: DebateManager
Tests: tests/test_debate_flow.py, tests/test_compaction.py
Invariants: At most one debate runs globally at any time; reconnecting clients receive in-progress turn buffers cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator
from typing import Any

from app.errors import AppError, DebateBusyError, DebateStateError, UpstreamLLMError
from app.repositories.debates_repo import DebatesRepository
from app.schemas import (
    ActiveTurnSnapshot,
    CompactionRecord,
    ContinueRequest,
    DebateRecord,
    MessageRecord,
    ResolvedSpeaker,
)
from app.services.compaction import CompactionService
from app.services.llm_client import OpenWeightGymClient
from app.services.prompt_builder import build_messages_for_speaker

logger = logging.getLogger(__name__)


class DebateManager:
    """Global single-debate runner managing background LLM turns, compaction, and browser SSE fan-out."""

    def __init__(
        self,
        repo: DebatesRepository,
        llm_client: OpenWeightGymClient,
        compaction_service: CompactionService | None = None,
    ) -> None:
        self._repo = repo
        self._llm_client = llm_client
        self._compaction = compaction_service or CompactionService(repo=repo, llm_client=llm_client)
        self._lock = asyncio.Lock()
        self._active_debate_id: str | None = None
        self._active_owner_id: str | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._active_turn: ActiveTurnSnapshot | None = None
        self._stop_mode: str | None = None
        self._pending_interjections: dict[str, list[str]] = {}
        self._pending_compaction: set[str] = set()
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    @property
    def active_debate_id(self) -> str | None:
        """Return the ID of the currently executing debate, or None if idle."""
        return self._active_debate_id

    @property
    def active_owner_id(self) -> str | None:
        """Return the owning user_id of the currently running debate, or None."""
        return self._active_owner_id

    def get_active_debate_id_for_user(self, user_id: str | None) -> str | None:
        """Return active_debate_id only if it belongs to `user_id` (prevents cross-user leakage)."""
        if self._active_debate_id is None:
            return None
        if user_id is None or self._active_owner_id == user_id:
            return self._active_debate_id
        return None

    def get_active_turn(self, debate_id: str) -> ActiveTurnSnapshot | None:
        """Return the buffered in-progress turn snapshot if `debate_id` is currently streaming."""
        if self._active_turn and self._active_turn.debate_id == debate_id:
            return self._active_turn.model_copy(deep=True)
        return None

    async def start_debate(self, debate_id: str, *, user_id: str | None = None) -> DebateRecord:
        """Start or resume a debate under the global single-debate lock."""
        async with self._lock:
            debate = self._repo.require_debate(debate_id, user_id=user_id)
            if self._active_debate_id is not None:
                if self._active_debate_id == debate_id:
                    return debate
                visible_active = (
                    self._active_debate_id
                    if (user_id is None or self._active_owner_id == user_id)
                    else None
                )
                raise DebateBusyError(active_debate_id=visible_active)

            if not debate.model_a_config.model_id.strip() or not debate.model_b_config.model_id.strip():
                raise AppError(
                    code="models_required",
                    message="Select models for both Model A and Model B before starting the debate.",
                    status_code=400,
                )

            resolved = debate.resolved_first_speaker
            if resolved is None:
                resolved = (
                    random.choice(["A", "B"])
                    if debate.first_speaker == "random"
                    else debate.first_speaker
                )

            for slot, cfg in (("A", debate.model_a_config), ("B", debate.model_b_config)):
                info = self._llm_client.get_cached_model(cfg.model_id)
                if info is not None:
                    self._repo.update_model_context(
                        debate_id,
                        slot,
                        loaded=info.loaded_context,
                        guaranteed=info.context_guaranteed,
                        max_ctx=info.context_max,
                    )

            updated = self._repo.update_runtime_state(
                debate_id,
                status="running",
                resolved_first_speaker=resolved,
                clear_error=True,
            )
            self._active_debate_id = debate_id
            self._active_owner_id = debate.user_id
            self._stop_mode = None
            self._active_task = asyncio.create_task(
                self._run_loop(debate_id),
                name=f"debate-runner-{debate_id}",
            )

        self._publish(debate_id, "status", {"debate": updated.model_dump()})
        return updated

    async def resume_debate(self, debate_id: str, *, user_id: str | None = None) -> DebateRecord:
        """Resume a paused or errored debate."""
        debate = self._repo.require_debate(debate_id, user_id=user_id)
        if debate.status == "limit_reached":
            raise DebateStateError(
                "This debate reached its turn or time limit. Use Continue to add turns or time.",
                code="limit_reached",
            )
        return await self.start_debate(debate_id, user_id=user_id)

    async def continue_debate(
        self,
        debate_id: str,
        req: ContinueRequest,
        *,
        user_id: str | None = None,
    ) -> DebateRecord:
        """Extend turn or time limits on a debate and resume execution."""
        debate = self._repo.require_debate(debate_id, user_id=user_id)
        if req.add_turns is None and req.add_seconds is None:
            raise AppError(
                code="extension_required",
                message="Specify add_turns or add_seconds to continue the debate.",
                status_code=400,
            )

        new_turn_limit = debate.turn_limit
        update_turns = False
        if req.add_turns is not None:
            completed = self._repo.count_model_turns(debate_id)
            base_turns = max(completed, debate.turn_limit or 0)
            new_turn_limit = base_turns + req.add_turns
            update_turns = True

        new_time_limit = debate.time_limit_seconds
        update_time = False
        if req.add_seconds is not None:
            base_secs = max(int(debate.elapsed_active_seconds), debate.time_limit_seconds or 0)
            new_time_limit = base_secs + req.add_seconds
            update_time = True

        self._repo.update_runtime_state(
            debate_id,
            turn_limit=new_turn_limit,
            update_turn_limit=update_turns,
            time_limit_seconds=new_time_limit,
            update_time_limit=update_time,
            clear_error=True,
        )
        return await self.start_debate(debate_id, user_id=user_id)

    async def pause_debate(self, debate_id: str, *, user_id: str | None = None) -> DebateRecord:
        """Pause the active debate, interrupting any in-flight stream cleanly."""
        return await self._halt_debate(debate_id, mode="pause", user_id=user_id)

    async def stop_debate(self, debate_id: str, *, user_id: str | None = None) -> DebateRecord:
        """Stop the debate and release the global active slot."""
        return await self._halt_debate(debate_id, mode="stop", user_id=user_id)

    async def force_stop_active(self) -> bool:
        """Admin emergency stop for any currently running debate without inspecting its content."""
        active_id = self._active_debate_id
        if active_id is None:
            return False
        await self._halt_debate(active_id, mode="stop", user_id=None)
        return True

    async def _halt_debate(
        self,
        debate_id: str,
        *,
        mode: str,
        user_id: str | None = None,
    ) -> DebateRecord:
        task_to_await: asyncio.Task[None] | None = None
        async with self._lock:
            self._repo.require_debate(debate_id, user_id=user_id)
            if self._active_debate_id == debate_id and self._active_task is not None:
                self._stop_mode = mode
                self._active_task.cancel()
                task_to_await = self._active_task
            else:
                target_status = "paused" if mode == "pause" else "stopped"
                updated = self._repo.update_runtime_state(debate_id, status=target_status)
                self._publish(debate_id, "status", {"debate": updated.model_dump()})
                return updated

        if task_to_await is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task_to_await
        return self._repo.require_debate(debate_id)

    async def compact_now(
        self,
        debate_id: str,
        *,
        user_id: str | None = None,
    ) -> tuple[bool, CompactionRecord | None]:
        """Run manual compaction immediately when idle, or queue it for the next turn boundary when running."""
        async with self._lock:
            self._repo.require_debate(debate_id, user_id=user_id)
            if self._active_debate_id == debate_id:
                self._pending_compaction.add(debate_id)
                self._publish(
                    debate_id,
                    "compaction_queued",
                    {"message": "Context compaction queued for the next turn boundary."},
                )
                return True, None

        compaction = await self._compaction.compact_debate(debate_id, trigger_reason="manual")
        if compaction is None:
            raise AppError(
                code="nothing_to_compact",
                message="Not enough older turns yet to compact beyond the recent turns window.",
                status_code=400,
            )
        self._publish(debate_id, "compaction", {"compaction": compaction.model_dump()})
        return False, compaction

    async def interject(
        self,
        debate_id: str,
        content: str,
        *,
        user_id: str | None = None,
    ) -> MessageRecord | None:
        """Add a moderator message immediately when idle, or queue it for the next turn when running."""
        cleaned = content.strip()
        if not cleaned:
            raise AppError(code="empty_interjection", message="Moderator message cannot be empty.")

        async with self._lock:
            self._repo.require_debate(debate_id, user_id=user_id)
            if self._active_debate_id == debate_id:
                self._pending_interjections.setdefault(debate_id, []).append(cleaned)
                self._publish(
                    debate_id,
                    "interjection_queued",
                    {"content": cleaned, "queued_count": len(self._pending_interjections[debate_id])},
                )
                return None

        msg = self._repo.add_message(debate_id, sender="moderator", content=cleaned)
        self._publish(debate_id, "message", {"message": msg.model_dump()})
        return msg

    async def subscribe(
        self,
        debate_id: str,
        *,
        user_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to live SSE events for a debate, yielding initial state and keepalives."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.setdefault(debate_id, set()).add(queue)
        try:
            debate = self._repo.require_debate(debate_id, user_id=user_id)
            yield {
                "event": "snapshot",
                "data": {
                    "debate": debate.model_dump(),
                    "active_turn": (
                        self._active_turn.model_dump()
                        if self._active_turn and self._active_turn.debate_id == debate_id
                        else None
                    ),
                    "global_active_debate_id": self.get_active_debate_id_for_user(user_id),
                },
            }
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield event
                except TimeoutError:
                    yield {"event": "ping", "data": {"ts": int(time.time())}}
        finally:
            subs = self._subscribers.get(debate_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(debate_id, None)

    async def wait_for_idle(self, timeout: float = 10.0) -> None:
        """Await completion of the currently active debate task."""
        task = self._active_task
        if task is not None and not task.done():
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def shutdown(self) -> None:
        """Gracefully cancel any running debate on server shutdown."""
        if self._active_debate_id is not None:
            await self.pause_debate(self._active_debate_id)

    def _publish(self, debate_id: str, event_name: str, data: dict[str, Any]) -> None:
        subs = self._subscribers.get(debate_id)
        if not subs:
            return
        payload = {"event": event_name, "data": data}
        for q in list(subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)

    def _next_speaker(self, debate: DebateRecord, history: list[MessageRecord]) -> ResolvedSpeaker:
        for msg in reversed(history):
            if msg.sender == "A":
                return "B"
            if msg.sender == "B":
                return "A"
        return debate.resolved_first_speaker or "A"

    async def _flush_pending_interjections(self, debate_id: str) -> None:
        pending = self._pending_interjections.pop(debate_id, [])
        for text in pending:
            msg = self._repo.add_message(debate_id, sender="moderator", content=text)
            self._publish(debate_id, "message", {"message": msg.model_dump()})

    async def _run_loop(self, debate_id: str) -> None:
        final_status: str | None = None
        final_error: str | None = None

        try:
            while True:
                await self._flush_pending_interjections(debate_id)
                debate = self._repo.require_debate(debate_id)
                history = self._repo.list_messages(debate_id)
                completed_turns = sum(1 for m in history if m.sender in ("A", "B"))

                if debate.turn_limit is not None and completed_turns >= debate.turn_limit:
                    final_status = "limit_reached"
                    break
                if (
                    debate.time_limit_seconds is not None
                    and debate.elapsed_active_seconds >= debate.time_limit_seconds
                ):
                    final_status = "limit_reached"
                    break

                speaker = self._next_speaker(debate, history)
                latest_compaction = self._repo.get_latest_compaction(debate_id)

                # Run queued manual compaction or proactive auto-compaction before this turn.
                manual_requested = debate_id in self._pending_compaction
                if manual_requested:
                    self._pending_compaction.discard(debate_id)
                    compacted = await self._compaction.compact_debate(
                        debate_id, trigger_reason="manual"
                    )
                    if compacted is not None:
                        latest_compaction = compacted
                        self._publish(debate_id, "compaction", {"compaction": compacted.model_dump()})
                elif self._compaction.should_auto_compact(debate, history, latest_compaction, speaker):
                    compacted = await self._compaction.compact_debate(
                        debate_id, trigger_reason="auto"
                    )
                    if compacted is not None:
                        latest_compaction = compacted
                        self._publish(debate_id, "compaction", {"compaction": compacted.model_dump()})

                participant = debate.model_a_config if speaker == "A" else debate.model_b_config
                messages = build_messages_for_speaker(
                    debate=debate,
                    history=history,
                    speaker=speaker,
                    latest_compaction=latest_compaction,
                )

                turn_index = (history[-1].turn_index + 1) if history else 1
                started_mono = time.monotonic()
                started_ms = int(time.time() * 1000)

                self._active_turn = ActiveTurnSnapshot(
                    debate_id=debate_id,
                    turn_index=turn_index,
                    speaker=speaker,
                    model_id=participant.model_id,
                    thinking="",
                    content="",
                    started_at_ms=started_ms,
                    status_hint=None,
                )
                self._publish(debate_id, "turn_start", self._active_turn.model_dump())

                turn_meta: dict[str, Any] = {}
                thinking_chunks: list[str] = []
                content_chunks: list[str] = []

                await self._stream_turn_with_recovery(
                    debate_id=debate_id,
                    debate=debate,
                    history=history,
                    speaker=speaker,
                    participant=participant,
                    messages=messages,
                    thinking_chunks=thinking_chunks,
                    content_chunks=content_chunks,
                    turn_meta=turn_meta,
                )

                duration_ms = max(1, int((time.monotonic() - started_mono) * 1000))
                completion_tokens = turn_meta.get("completion_tokens")
                tok_per_sec = (
                    round(completion_tokens / (duration_ms / 1000.0), 2)
                    if isinstance(completion_tokens, int) and completion_tokens > 0
                    else None
                )
                loaded_ctx = turn_meta.get("loaded_context_tokens")
                if isinstance(loaded_ctx, int):
                    self._repo.update_model_context(debate_id, speaker, loaded=loaded_ctx)

                saved_msg = self._repo.add_message(
                    debate_id,
                    sender=speaker,
                    model_id=participant.model_id,
                    answered_model_id=turn_meta.get("answered_model_id"),
                    answered_node=turn_meta.get("answered_node"),
                    content="".join(content_chunks),
                    thinking="".join(thinking_chunks),
                    prompt_tokens=turn_meta.get("prompt_tokens"),
                    completion_tokens=completion_tokens,
                    loaded_context_tokens=loaded_ctx,
                    duration_ms=duration_ms,
                    tokens_per_sec=tok_per_sec,
                    request_id=turn_meta.get("request_id"),
                    loadcoach_request_id=turn_meta.get("loadcoach_request_id"),
                    finish_reason=turn_meta.get("finish_reason"),
                )

                refreshed = self._repo.update_runtime_state(
                    debate_id,
                    elapsed_active_seconds=debate.elapsed_active_seconds + (duration_ms / 1000.0),
                )
                self._active_turn = None
                self._publish(
                    debate_id,
                    "turn_complete",
                    {
                        "message": saved_msg.model_dump(),
                        "debate": refreshed.model_dump(),
                    },
                )

                if turn_meta.get("incomplete"):
                    final_status = "paused"
                    final_error = (
                        "Stream ended before completion (partial turn saved). "
                        "You can resume to continue the debate."
                    )
                    break

                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            if self._active_turn and (self._active_turn.content or self._active_turn.thinking):
                saved_partial = self._repo.add_message(
                    debate_id,
                    sender=self._active_turn.speaker,
                    model_id=self._active_turn.model_id,
                    content=self._active_turn.content,
                    thinking=self._active_turn.thinking,
                    finish_reason="cancelled",
                )
                self._publish(debate_id, "message", {"message": saved_partial.model_dump()})
            final_status = "stopped" if self._stop_mode == "stop" else "paused"
        except UpstreamLLMError as exc:
            if (
                exc.upstream_code == "context_too_small"
                and exc.loaded_context
                and self._active_turn
            ):
                self._repo.update_model_context(
                    debate_id,
                    self._active_turn.speaker,
                    loaded=exc.loaded_context,
                    max_ctx=exc.max_context,
                )

            err_payload = dict(exc.to_payload()["error"])
            if exc.upstream_status == 404:
                with contextlib.suppress(Exception):
                    await self._llm_client.list_chat_models()
                err_payload["refresh_models"] = True
                final_status = "paused"
                final_error = (
                    f"{exc.message} Available models list has been refreshed — "
                    "verify your model selection and resume."
                )
            elif exc.upstream_status in (429, 503):
                hints: list[str] = []
                if exc.turnstile:
                    hints.append(f"gateway status: {exc.turnstile}")
                if exc.retry_after is not None:
                    hints.append(f"retry after ~{exc.retry_after}s")
                suffix = f" ({', '.join(hints)})" if hints else ""
                final_status = "paused"
                final_error = f"{exc.message}{suffix} Debate paused — click Resume to retry."
            elif exc.upstream_status in (400, 409, 413):
                final_status = "paused"
                final_error = exc.message
            else:
                final_status = "error"
                final_error = exc.message

            err_payload["message"] = final_error
            self._publish(debate_id, "error", err_payload)
        except Exception as exc:
            logger.exception("Unexpected error in debate runner for %s", debate_id)
            final_status = "error"
            final_error = f"Unexpected debate error: {exc}"
            self._publish(
                debate_id,
                "error",
                {"code": "internal_error", "message": final_error},
            )
        finally:
            self._active_turn = None
            async with self._lock:
                if self._active_debate_id == debate_id:
                    self._active_debate_id = None
                    self._active_owner_id = None
                    self._active_task = None
                    self._stop_mode = None

            updated = self._repo.update_runtime_state(
                debate_id,
                status=final_status or "paused",  # type: ignore[arg-type]
                last_error=final_error,
                clear_error=(final_error is None),
            )
            self._publish(debate_id, "status", {"debate": updated.model_dump()})

    async def _stream_turn_with_recovery(
        self,
        *,
        debate_id: str,
        debate: DebateRecord,
        history: list[MessageRecord],
        speaker: ResolvedSpeaker,
        participant: Any,
        messages: list[dict[str, str]],
        thinking_chunks: list[str],
        content_chunks: list[str],
        turn_meta: dict[str, Any],
    ) -> None:
        transient_retried = False
        compaction_retried = False
        current_messages = messages

        while True:
            try:
                async for ev in self._llm_client.stream_chat_completion(
                    participant=participant,
                    messages=current_messages,
                    trace_id=f"debate-{debate_id}",
                ):
                    if ev.kind == "meta":
                        turn_meta.update(ev.meta)
                        if self._active_turn and ev.meta.get("estimated_wait"):
                            self._active_turn.status_hint = f"Queued (~{ev.meta['estimated_wait']}s wait)..."
                        self._publish(debate_id, "turn_meta", {"meta": ev.meta})
                    elif ev.kind == "thinking":
                        thinking_chunks.append(ev.text)
                        if self._active_turn:
                            self._active_turn.thinking += ev.text
                            self._active_turn.status_hint = None
                        self._publish(
                            debate_id,
                            "thinking_delta",
                            {"speaker": speaker, "text": ev.text},
                        )
                    elif ev.kind == "content":
                        content_chunks.append(ev.text)
                        if self._active_turn:
                            self._active_turn.content += ev.text
                            self._active_turn.status_hint = None
                        self._publish(
                            debate_id,
                            "content_delta",
                            {"speaker": speaker, "text": ev.text},
                        )
                    elif ev.kind == "done":
                        turn_meta.update(ev.meta)
                return
            except UpstreamLLMError as exc:
                # Reactive 400 context_too_small recovery: update window, auto-compact, retry once.
                if (
                    exc.upstream_status == 400
                    and exc.upstream_code == "context_too_small"
                    and not compaction_retried
                    and not thinking_chunks
                    and not content_chunks
                ):
                    compaction_retried = True
                    if exc.loaded_context:
                        self._repo.update_model_context(
                            debate_id,
                            speaker,
                            loaded=exc.loaded_context,
                            max_ctx=exc.max_context,
                        )
                    if self._active_turn:
                        self._active_turn.status_hint = "Context full — compacting and retrying..."
                    self._publish(
                        debate_id,
                        "turn_meta",
                        {"meta": {"status_hint": "Context full — compacting and retrying..."}},
                    )
                    compacted = await self._compaction.compact_debate(
                        debate_id,
                        trigger_reason="context_too_small",
                    )
                    if compacted is not None:
                        self._publish(debate_id, "compaction", {"compaction": compacted.model_dump()})
                        refreshed_debate = self._repo.require_debate(debate_id)
                        current_messages = build_messages_for_speaker(
                            debate=refreshed_debate,
                            history=history,
                            speaker=speaker,
                            latest_compaction=compacted,
                        )
                        continue
                    raise

                # Single automatic retry on transient 502/504 before any tokens have arrived.
                if (
                    exc.upstream_status in (502, 504)
                    and not transient_retried
                    and not thinking_chunks
                    and not content_chunks
                ):
                    transient_retried = True
                    if self._active_turn:
                        self._active_turn.status_hint = "Engine transient error; retrying once..."
                    self._publish(
                        debate_id,
                        "turn_meta",
                        {"meta": {"status_hint": "Engine transient error; retrying once..."}},
                    )
                    await asyncio.sleep(1.0)
                    continue
                raise
