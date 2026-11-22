"""
Purpose: SQLite repository for creating, querying, updating, compacting, forking, and deleting debates and messages.
Role: repository
Patterns: repository-pattern
Depends on: app.repositories.db, app.schemas, app.errors, sqlite3, uuid
Exposes: DebatesRepository
Tests: tests/test_debate_flow.py, tests/test_compaction.py, tests/test_auth_and_roles.py
Invariants: All SQL queries use bound parameters; original messages are never deleted on compaction; user_id ownership is enforced when provided.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.errors import DebateNotFoundError
from app.repositories.db import get_connection
from app.schemas import (
    CompactionRecord,
    CompactionTrigger,
    DebateCreate,
    DebateRecord,
    DebateStatus,
    DebateUpdate,
    MessageRecord,
    MessageSender,
    ModelParticipantConfig,
    NarratorConfig,
    ResolvedSpeaker,
    TurnSpeaker,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_debate(row: sqlite3.Row, message_count: int | None = None) -> DebateRecord:
    keys = set(row.keys())
    count = (
        int(row["message_count"])
        if "message_count" in keys and row["message_count"] is not None
        else (message_count or 0)
    )
    raw_narrator = row["narrator_config"] if "narrator_config" in keys and row["narrator_config"] else "{}"
    return DebateRecord(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        scene_prompt=row["scene_prompt"],
        first_speaker=row["first_speaker"],
        resolved_first_speaker=row["resolved_first_speaker"],
        status=row["status"],
        turn_limit=row["turn_limit"],
        time_limit_seconds=row["time_limit_seconds"],
        elapsed_active_seconds=float(row["elapsed_active_seconds"] or 0.0),
        auto_compact_threshold=float(
            row["auto_compact_threshold"] if "auto_compact_threshold" in keys else 0.78
        ),
        keep_recent_turns=int(row["keep_recent_turns"] if "keep_recent_turns" in keys else 4),
        show_thinking=bool(row["show_thinking"]),
        model_a_config=ModelParticipantConfig.model_validate_json(row["model_a_config"]),
        model_b_config=ModelParticipantConfig.model_validate_json(row["model_b_config"]),
        model_a_ctx_loaded=row["model_a_ctx_loaded"],
        model_a_ctx_guaranteed=row["model_a_ctx_guaranteed"],
        model_a_ctx_max=row["model_a_ctx_max"],
        model_b_ctx_loaded=row["model_b_ctx_loaded"],
        model_b_ctx_guaranteed=row["model_b_ctx_guaranteed"],
        model_b_ctx_max=row["model_b_ctx_max"],
        narrator_enabled=bool(row["narrator_enabled"]) if "narrator_enabled" in keys else False,
        narrator_config=NarratorConfig.model_validate_json(raw_narrator),
        narrator_cadence_mode=(
            row["narrator_cadence_mode"]
            if "narrator_cadence_mode" in keys and row["narrator_cadence_mode"]
            else "every_turn"
        ),
        narrator_cadence_n=int(
            row["narrator_cadence_n"]
            if "narrator_cadence_n" in keys and row["narrator_cadence_n"] is not None
            else 1
        ),
        narrator_turns_until_next=(
            row["narrator_turns_until_next"] if "narrator_turns_until_next" in keys else None
        ),
        narrator_ctx_loaded=row["narrator_ctx_loaded"] if "narrator_ctx_loaded" in keys else None,
        narrator_ctx_guaranteed=(
            row["narrator_ctx_guaranteed"] if "narrator_ctx_guaranteed" in keys else None
        ),
        narrator_ctx_max=row["narrator_ctx_max"] if "narrator_ctx_max" in keys else None,
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=count,
    )


def _row_to_message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        id=row["id"],
        debate_id=row["debate_id"],
        turn_index=int(row["turn_index"]),
        sender=row["sender"],
        model_id=row["model_id"],
        answered_model_id=row["answered_model_id"],
        answered_node=row["answered_node"],
        content=row["content"],
        thinking=row["thinking"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        loaded_context_tokens=row["loaded_context_tokens"],
        duration_ms=row["duration_ms"],
        tokens_per_sec=row["tokens_per_sec"],
        request_id=row["request_id"],
        loadcoach_request_id=row["loadcoach_request_id"],
        finish_reason=row["finish_reason"],
        created_at=row["created_at"],
    )


def _row_to_compaction(row: sqlite3.Row) -> CompactionRecord:
    return CompactionRecord(
        id=row["id"],
        debate_id=row["debate_id"],
        through_message_id=row["through_message_id"],
        summary_text=row["summary_text"],
        summarizer_model_id=row["summarizer_model_id"],
        trigger_reason=row["trigger_reason"],
        created_at=row["created_at"],
    )


class DebatesRepository:
    """Persistence facade for debates, transcript messages, and context compactions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def create_debate(self, payload: DebateCreate, *, user_id: str | None = None) -> DebateRecord:
        """Insert a new draft debate into SQLite.

        Parameters:
            payload: Validated DebateCreate DTO.
            user_id: Optional owning user ID.
        Returns:
            Newly created DebateRecord.
        Side effects:
            Writes one row to the debates table.
        """
        debate_id = f"deb_{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO debates (
                    id, user_id, title, scene_prompt, first_speaker, resolved_first_speaker,
                    status, turn_limit, time_limit_seconds, elapsed_active_seconds,
                    auto_compact_threshold, keep_recent_turns, show_thinking,
                    model_a_config, model_b_config,
                    narrator_enabled, narrator_config, narrator_cadence_mode,
                    narrator_cadence_n, narrator_turns_until_next,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'draft', ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    debate_id,
                    user_id,
                    payload.title.strip(),
                    payload.scene_prompt,
                    payload.first_speaker,
                    payload.turn_limit,
                    payload.time_limit_seconds,
                    payload.auto_compact_threshold,
                    payload.keep_recent_turns,
                    1 if payload.show_thinking else 0,
                    payload.model_a_config.model_dump_json(),
                    payload.model_b_config.model_dump_json(),
                    1 if payload.narrator_enabled else 0,
                    payload.narrator_config.model_dump_json(),
                    payload.narrator_cadence_mode,
                    payload.narrator_cadence_n,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM debates WHERE id = ?", (debate_id,)).fetchone()
        return _row_to_debate(row, message_count=0)

    def get_debate(self, debate_id: str, *, user_id: str | None = None) -> DebateRecord | None:
        """Fetch one debate by ID, optionally enforcing user ownership."""
        with get_connection(self.db_path) as conn:
            if user_id is not None:
                row = conn.execute(
                    """
                    SELECT d.*, (SELECT COUNT(*) FROM messages m WHERE m.debate_id = d.id) AS message_count
                    FROM debates d
                    WHERE d.id = ? AND d.user_id = ?
                    """,
                    (debate_id, user_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT d.*, (SELECT COUNT(*) FROM messages m WHERE m.debate_id = d.id) AS message_count
                    FROM debates d
                    WHERE d.id = ?
                    """,
                    (debate_id,),
                ).fetchone()
        return _row_to_debate(row) if row is not None else None

    def require_debate(self, debate_id: str, *, user_id: str | None = None) -> DebateRecord:
        """Fetch one debate by ID (optionally verifying ownership) or raise DebateNotFoundError."""
        debate = self.get_debate(debate_id, user_id=user_id)
        if debate is None:
            raise DebateNotFoundError(debate_id)
        return debate

    def list_debates(self, *, user_id: str | None = None) -> list[DebateRecord]:
        """List debates ordered by most recently updated, optionally filtered by user_id."""
        with get_connection(self.db_path) as conn:
            if user_id is not None:
                rows = conn.execute(
                    """
                    SELECT d.*, (SELECT COUNT(*) FROM messages m WHERE m.debate_id = d.id) AS message_count
                    FROM debates d
                    WHERE d.user_id = ?
                    ORDER BY d.updated_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT d.*, (SELECT COUNT(*) FROM messages m WHERE m.debate_id = d.id) AS message_count
                    FROM debates d
                    ORDER BY d.updated_at DESC
                    """
                ).fetchall()
        return [_row_to_debate(r) for r in rows]

    def update_debate(
        self,
        debate_id: str,
        patch: DebateUpdate,
        *,
        user_id: str | None = None,
    ) -> DebateRecord:
        """Update editable debate configuration fields."""
        current = self.require_debate(debate_id, user_id=user_id)
        fields_set = patch.model_fields_set

        title = patch.title.strip() if "title" in fields_set and patch.title is not None else current.title
        scene_prompt = (
            patch.scene_prompt
            if "scene_prompt" in fields_set and patch.scene_prompt is not None
            else current.scene_prompt
        )
        first_speaker = (
            patch.first_speaker
            if "first_speaker" in fields_set and patch.first_speaker is not None
            else current.first_speaker
        )
        turn_limit = patch.turn_limit if "turn_limit" in fields_set else current.turn_limit
        time_limit_seconds = (
            patch.time_limit_seconds if "time_limit_seconds" in fields_set else current.time_limit_seconds
        )
        auto_compact_threshold = (
            patch.auto_compact_threshold
            if "auto_compact_threshold" in fields_set and patch.auto_compact_threshold is not None
            else current.auto_compact_threshold
        )
        keep_recent_turns = (
            patch.keep_recent_turns
            if "keep_recent_turns" in fields_set and patch.keep_recent_turns is not None
            else current.keep_recent_turns
        )
        show_thinking = (
            patch.show_thinking
            if "show_thinking" in fields_set and patch.show_thinking is not None
            else current.show_thinking
        )
        model_a_config = (
            patch.model_a_config
            if "model_a_config" in fields_set and patch.model_a_config is not None
            else current.model_a_config
        )
        model_b_config = (
            patch.model_b_config
            if "model_b_config" in fields_set and patch.model_b_config is not None
            else current.model_b_config
        )
        narrator_enabled = (
            patch.narrator_enabled
            if "narrator_enabled" in fields_set and patch.narrator_enabled is not None
            else current.narrator_enabled
        )
        narrator_config = (
            patch.narrator_config
            if "narrator_config" in fields_set and patch.narrator_config is not None
            else current.narrator_config
        )
        narrator_cadence_mode = (
            patch.narrator_cadence_mode
            if "narrator_cadence_mode" in fields_set and patch.narrator_cadence_mode is not None
            else current.narrator_cadence_mode
        )
        narrator_cadence_n = (
            patch.narrator_cadence_n
            if "narrator_cadence_n" in fields_set and patch.narrator_cadence_n is not None
            else current.narrator_cadence_n
        )
        cadence_changed = (
            narrator_enabled != current.narrator_enabled
            or narrator_cadence_mode != current.narrator_cadence_mode
            or narrator_cadence_n != current.narrator_cadence_n
        )
        narrator_turns_until_next = None if cadence_changed else current.narrator_turns_until_next

        now = _utc_now()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE debates
                SET title = ?, scene_prompt = ?, first_speaker = ?, turn_limit = ?,
                    time_limit_seconds = ?, auto_compact_threshold = ?, keep_recent_turns = ?,
                    show_thinking = ?, model_a_config = ?, model_b_config = ?,
                    narrator_enabled = ?, narrator_config = ?, narrator_cadence_mode = ?,
                    narrator_cadence_n = ?, narrator_turns_until_next = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    scene_prompt,
                    first_speaker,
                    turn_limit,
                    time_limit_seconds,
                    auto_compact_threshold,
                    keep_recent_turns,
                    1 if show_thinking else 0,
                    model_a_config.model_dump_json(),
                    model_b_config.model_dump_json(),
                    1 if narrator_enabled else 0,
                    narrator_config.model_dump_json(),
                    narrator_cadence_mode,
                    narrator_cadence_n,
                    narrator_turns_until_next,
                    now,
                    debate_id,
                ),
            )
        return self.require_debate(debate_id, user_id=user_id)

    def update_runtime_state(
        self,
        debate_id: str,
        *,
        status: DebateStatus | None = None,
        resolved_first_speaker: ResolvedSpeaker | None = None,
        elapsed_active_seconds: float | None = None,
        turn_limit: int | None = None,
        update_turn_limit: bool = False,
        time_limit_seconds: int | None = None,
        update_time_limit: bool = False,
        narrator_turns_until_next: int | None = None,
        update_narrator_countdown: bool = False,
        last_error: str | None = None,
        clear_error: bool = False,
    ) -> DebateRecord:
        """Update lifecycle status, runtime timers, narrator countdown, resolved speaker, or last error."""
        current = self.require_debate(debate_id)
        new_status = status if status is not None else current.status
        new_resolved = (
            resolved_first_speaker
            if resolved_first_speaker is not None
            else current.resolved_first_speaker
        )
        new_elapsed = (
            elapsed_active_seconds
            if elapsed_active_seconds is not None
            else current.elapsed_active_seconds
        )
        new_turn_limit = turn_limit if update_turn_limit else current.turn_limit
        new_time_limit = time_limit_seconds if update_time_limit else current.time_limit_seconds
        new_narrator_cd = (
            narrator_turns_until_next
            if update_narrator_countdown
            else current.narrator_turns_until_next
        )
        if clear_error:
            new_error = None
        elif last_error is not None:
            new_error = last_error
        else:
            new_error = current.last_error

        now = _utc_now()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE debates
                SET status = ?, resolved_first_speaker = ?, elapsed_active_seconds = ?,
                    turn_limit = ?, time_limit_seconds = ?, narrator_turns_until_next = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    new_resolved,
                    new_elapsed,
                    new_turn_limit,
                    new_time_limit,
                    new_narrator_cd,
                    new_error,
                    now,
                    debate_id,
                ),
            )
        return self.require_debate(debate_id)

    def update_model_context(
        self,
        debate_id: str,
        speaker: TurnSpeaker,
        *,
        loaded: int | None = None,
        guaranteed: int | None = None,
        max_ctx: int | None = None,
    ) -> None:
        """Update observed context window metadata for Model A, Model B, or Narrator."""
        current = self.require_debate(debate_id)
        now = _utc_now()
        if speaker == "A":
            new_loaded = loaded if loaded is not None else current.model_a_ctx_loaded
            new_guaranteed = guaranteed if guaranteed is not None else current.model_a_ctx_guaranteed
            new_max = max_ctx if max_ctx is not None else current.model_a_ctx_max
            sql = """
                UPDATE debates
                SET model_a_ctx_loaded = ?, model_a_ctx_guaranteed = ?, model_a_ctx_max = ?, updated_at = ?
                WHERE id = ?
            """
        elif speaker == "B":
            new_loaded = loaded if loaded is not None else current.model_b_ctx_loaded
            new_guaranteed = guaranteed if guaranteed is not None else current.model_b_ctx_guaranteed
            new_max = max_ctx if max_ctx is not None else current.model_b_ctx_max
            sql = """
                UPDATE debates
                SET model_b_ctx_loaded = ?, model_b_ctx_guaranteed = ?, model_b_ctx_max = ?, updated_at = ?
                WHERE id = ?
            """
        else:
            new_loaded = loaded if loaded is not None else current.narrator_ctx_loaded
            new_guaranteed = guaranteed if guaranteed is not None else current.narrator_ctx_guaranteed
            new_max = max_ctx if max_ctx is not None else current.narrator_ctx_max
            sql = """
                UPDATE debates
                SET narrator_ctx_loaded = ?, narrator_ctx_guaranteed = ?, narrator_ctx_max = ?, updated_at = ?
                WHERE id = ?
            """
        with get_connection(self.db_path) as conn:
            conn.execute(sql, (new_loaded, new_guaranteed, new_max, now, debate_id))

    def delete_debate(self, debate_id: str, *, user_id: str | None = None) -> None:
        """Delete a debate and its cascaded messages/compactions."""
        with get_connection(self.db_path) as conn:
            if user_id is not None:
                cur = conn.execute(
                    "DELETE FROM debates WHERE id = ? AND user_id = ?",
                    (debate_id, user_id),
                )
            else:
                cur = conn.execute("DELETE FROM debates WHERE id = ?", (debate_id,))
            if cur.rowcount == 0:
                raise DebateNotFoundError(debate_id)

    def add_message(
        self,
        debate_id: str,
        *,
        sender: MessageSender,
        content: str,
        thinking: str = "",
        model_id: str | None = None,
        answered_model_id: str | None = None,
        answered_node: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        loaded_context_tokens: int | None = None,
        duration_ms: int | None = None,
        tokens_per_sec: float | None = None,
        request_id: str | None = None,
        loadcoach_request_id: str | None = None,
        finish_reason: str | None = None,
    ) -> MessageRecord:
        """Append a message to a debate transcript with monotonically increasing turn_index."""
        self.require_debate(debate_id)
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_index), 0) AS max_idx FROM messages WHERE debate_id = ?",
                (debate_id,),
            ).fetchone()
            turn_index = int(row["max_idx"]) + 1
            conn.execute(
                """
                INSERT INTO messages (
                    id, debate_id, turn_index, sender, model_id, answered_model_id,
                    answered_node, content, thinking, prompt_tokens, completion_tokens,
                    loaded_context_tokens, duration_ms, tokens_per_sec, request_id,
                    loadcoach_request_id, finish_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    debate_id,
                    turn_index,
                    sender,
                    model_id,
                    answered_model_id,
                    answered_node,
                    content,
                    thinking,
                    prompt_tokens,
                    completion_tokens,
                    loaded_context_tokens,
                    duration_ms,
                    tokens_per_sec,
                    request_id,
                    loadcoach_request_id,
                    finish_reason,
                    now,
                ),
            )
            conn.execute("UPDATE debates SET updated_at = ? WHERE id = ?", (now, debate_id))
            inserted = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
        return _row_to_message(inserted)

    def list_messages(self, debate_id: str) -> list[MessageRecord]:
        """Return all transcript messages for a debate ordered by turn_index."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE debate_id = ? ORDER BY turn_index ASC, created_at ASC",
                (debate_id,),
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def count_model_turns(self, debate_id: str) -> int:
        """Count how many A/B model turns (excluding moderator interjections) have completed."""
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE debate_id = ? AND sender IN ('A', 'B')",
                (debate_id,),
            ).fetchone()
        return int(row["cnt"])

    def add_compaction(
        self,
        debate_id: str,
        *,
        through_message_id: str,
        summary_text: str,
        summarizer_model_id: str,
        trigger_reason: CompactionTrigger,
    ) -> CompactionRecord:
        """Persist a new context compaction summary without deleting any original messages."""
        self.require_debate(debate_id)
        comp_id = f"cmp_{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO compactions (
                    id, debate_id, through_message_id, summary_text,
                    summarizer_model_id, trigger_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comp_id,
                    debate_id,
                    through_message_id,
                    summary_text.strip(),
                    summarizer_model_id,
                    trigger_reason,
                    now,
                ),
            )
            conn.execute("UPDATE debates SET updated_at = ? WHERE id = ?", (now, debate_id))
            row = conn.execute("SELECT * FROM compactions WHERE id = ?", (comp_id,)).fetchone()
        return _row_to_compaction(row)

    def list_compactions(self, debate_id: str) -> list[CompactionRecord]:
        """Return all compactions for a debate ordered chronologically."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM compactions WHERE debate_id = ? ORDER BY created_at ASC",
                (debate_id,),
            ).fetchall()
        return [_row_to_compaction(r) for r in rows]

    def get_latest_compaction(self, debate_id: str) -> CompactionRecord | None:
        """Return the most recent compaction for a debate, or None."""
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM compactions WHERE debate_id = ? ORDER BY created_at DESC LIMIT 1",
                (debate_id,),
            ).fetchone()
        return _row_to_compaction(row) if row is not None else None

    def fork_debate(
        self,
        debate_id: str,
        *,
        swap_sides: bool = False,
        title: str | None = None,
        user_id: str | None = None,
    ) -> DebateRecord:
        """Create a fresh draft debate duplicating an existing debate's configuration."""
        source = self.require_debate(debate_id, user_id=user_id)
        cfg_a = source.model_a_config.model_copy(deep=True)
        cfg_b = source.model_b_config.model_copy(deep=True)
        if swap_sides:
            cfg_a.side, cfg_b.side = cfg_b.side, cfg_a.side

        new_title = title.strip() if title and title.strip() else f"{source.title} (Fork)"
        return self.create_debate(
            DebateCreate(
                title=new_title,
                scene_prompt=source.scene_prompt,
                first_speaker=source.first_speaker,
                turn_limit=source.turn_limit,
                time_limit_seconds=source.time_limit_seconds,
                auto_compact_threshold=source.auto_compact_threshold,
                keep_recent_turns=source.keep_recent_turns,
                show_thinking=source.show_thinking,
                model_a_config=cfg_a,
                model_b_config=cfg_b,
                narrator_enabled=source.narrator_enabled,
                narrator_config=source.narrator_config.model_copy(deep=True),
                narrator_cadence_mode=source.narrator_cadence_mode,
                narrator_cadence_n=source.narrator_cadence_n,
            ),
            user_id=user_id if user_id is not None else source.user_id,
        )
