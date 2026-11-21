"""
Purpose: SQLite repository for per-user custom debate prompt presets with strict ownership scoping.
Role: repository
Patterns: repository-pattern, structured-errors
Depends on: sqlite3, uuid, datetime, pathlib, app.errors, app.repositories.db, app.schemas
Exposes: PromptsRepository
Tests: tests/test_auth_and_roles.py
Invariants: All reads, updates, and deletes are strictly scoped by user_id.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.errors import ResourceNotFoundError
from app.repositories.db import get_connection
from app.schemas import CustomPromptCreate, CustomPromptRecord, CustomPromptUpdate


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_prompt(row: sqlite3.Row) -> CustomPromptRecord:
    return CustomPromptRecord(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        style_category=row["style_category"],
        content=row["content"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PromptsRepository:
    """Persistence layer for user-saved custom style prompts."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def list_prompts(self, user_id: str) -> list[CustomPromptRecord]:
        """List all custom prompts owned by `user_id`."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM custom_prompts WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [_row_to_prompt(r) for r in rows]

    def create_prompt(self, user_id: str, payload: CustomPromptCreate) -> CustomPromptRecord:
        """Create a new custom prompt belonging to `user_id`."""
        prompt_id = f"prm_{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO custom_prompts (id, user_id, name, style_category, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt_id,
                    user_id,
                    payload.name.strip(),
                    payload.style_category,
                    payload.content.strip(),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM custom_prompts WHERE id = ?", (prompt_id,)).fetchone()
        return _row_to_prompt(row)

    def update_prompt(
        self,
        user_id: str,
        prompt_id: str,
        patch: CustomPromptUpdate,
    ) -> CustomPromptRecord:
        """Update an existing custom prompt if owned by `user_id`."""
        with get_connection(self.db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM custom_prompts WHERE id = ? AND user_id = ?",
                (prompt_id, user_id),
            ).fetchone()
            if existing is None:
                raise ResourceNotFoundError("Custom prompt not found.")

            name = patch.name.strip() if patch.name is not None else existing["name"]
            category = patch.style_category if patch.style_category is not None else existing["style_category"]
            content = patch.content.strip() if patch.content is not None else existing["content"]
            now = _utc_now()
            conn.execute(
                """
                UPDATE custom_prompts
                SET name = ?, style_category = ?, content = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (name, category, content, now, prompt_id, user_id),
            )
            row = conn.execute("SELECT * FROM custom_prompts WHERE id = ?", (prompt_id,)).fetchone()
        return _row_to_prompt(row)

    def delete_prompt(self, user_id: str, prompt_id: str) -> None:
        """Delete a custom prompt if owned by `user_id`."""
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM custom_prompts WHERE id = ? AND user_id = ?",
                (prompt_id, user_id),
            )
            if cur.rowcount == 0:
                raise ResourceNotFoundError("Custom prompt not found.")
