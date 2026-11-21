"""
Purpose: SQLite repository for user accounts, first-user admin bootstrap, preferences, and session lifecycle.
Role: repository
Patterns: repository-pattern, structured-errors
Depends on: sqlite3, uuid, datetime, pathlib, app.errors, app.repositories.db, app.schemas, app.security
Exposes: UsersRepository
Tests: tests/test_auth_and_roles.py
Invariants: First registered user is atomically promoted to admin; password hashes and raw session tokens are never returned in UserRecord.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.errors import AppError, ResourceNotFoundError
from app.repositories.db import get_connection
from app.schemas import AdminUserUpdate, UserPreferencesUpdate, UserRecord, UserRole
from app.security import generate_session_token, hash_password, hash_session_token, verify_password

SESSION_TTL_DAYS = 14


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        theme=row["theme"],
        show_thinking_default=bool(row["show_thinking_default"]),
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


class UsersRepository:
    """SQLite persistence repository for users and session tokens."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def count_users(self) -> int:
        """Return total number of registered user accounts."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
        return int(row["cnt"])

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: UserRole | None = None,
    ) -> UserRecord:
        """Create a new user account, promoting the very first account to admin automatically.

        Parameters:
            username: Unique username string.
            password: Plaintext password (hashed with scrypt before persistence).
            role: Explicit role override (`admin` or `user`); when None, first user gets `admin`.
        Returns:
            Public UserRecord without password hash.
        Side effects:
            Inserts a row into the `users` table.
        """
        cleaned_name = username.strip()
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        pw_hash = hash_password(password)
        now = _utc_now().isoformat()

        with get_connection(self.db_path) as conn:
            existing_count = int(conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"])
            assigned_role: UserRole = role if role is not None else ("admin" if existing_count == 0 else "user")
            try:
                conn.execute(
                    """
                    INSERT INTO users (id, username, password_hash, role, theme, show_thinking_default, is_active, created_at)
                    VALUES (?, ?, ?, ?, 'obsidian', 1, 1, ?)
                    """,
                    (user_id, cleaned_name, pw_hash, assigned_role, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AppError(
                    code="username_taken",
                    message=f"Username '{cleaned_name}' is already registered.",
                    status_code=409,
                ) from exc
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row)

    def authenticate(self, *, username: str, password: str) -> UserRecord | None:
        """Verify username and password; returns active UserRecord or None."""
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE lower(username) = lower(?)",
                (username.strip(),),
            ).fetchone()
        if row is None or not bool(row["is_active"]):
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return _row_to_user(row)

    def get_by_id(self, user_id: str) -> UserRecord | None:
        """Fetch one user by ID."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row) if row is not None else None

    def list_users(self) -> list[UserRecord]:
        """List all user accounts ordered by creation date."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [_row_to_user(r) for r in rows]

    def update_preferences(self, user_id: str, patch: UserPreferencesUpdate) -> UserRecord:
        """Update a user's theme and default thinking visibility preference."""
        current = self.get_by_id(user_id)
        if current is None:
            raise ResourceNotFoundError("User not found.")
        theme = patch.theme if patch.theme is not None else current.theme
        show_thinking = (
            patch.show_thinking_default
            if patch.show_thinking_default is not None
            else current.show_thinking_default
        )
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET theme = ?, show_thinking_default = ? WHERE id = ?",
                (theme, 1 if show_thinking else 0, user_id),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row)

    def admin_update_user(self, user_id: str, patch: AdminUserUpdate) -> UserRecord:
        """Update role, active flag, or password for a user account (admin operation)."""
        current = self.get_by_id(user_id)
        if current is None:
            raise ResourceNotFoundError("User not found.")

        new_role = patch.role if patch.role is not None else current.role
        new_active = patch.is_active if patch.is_active is not None else current.is_active

        with get_connection(self.db_path) as conn:
            if patch.password is not None:
                conn.execute(
                    "UPDATE users SET role = ?, is_active = ?, password_hash = ? WHERE id = ?",
                    (new_role, 1 if new_active else 0, hash_password(patch.password), user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET role = ?, is_active = ? WHERE id = ?",
                    (new_role, 1 if new_active else 0, user_id),
                )
            if not new_active:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row)

    def create_session(self, user_id: str) -> str:
        """Create a persistent login session and return the raw session token."""
        token = generate_session_token()
        token_hash = hash_session_token(token)
        now = _utc_now()
        expires = now + timedelta(days=SESSION_TTL_DAYS)
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, expires.isoformat(), now.isoformat()),
            )
        return token

    def get_user_by_session_token(self, token: str) -> UserRecord | None:
        """Resolve a raw session token to an active UserRecord if valid and unexpired."""
        token_hash = hash_session_token(token)
        now_iso = _utc_now().isoformat()
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT u.*
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
                """,
                (token_hash, now_iso),
            ).fetchone()
        return _row_to_user(row) if row is not None else None

    def delete_session(self, token: str) -> None:
        """Revoke one session by raw token."""
        token_hash = hash_session_token(token)
        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
