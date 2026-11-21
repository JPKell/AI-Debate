"""
Purpose: SQLite WAL connection factory, full schema initialization, idempotent migrations, and crash-recovery helpers.
Role: repository
Patterns: repository-pattern
Depends on: sqlite3, pathlib, contextlib, datetime
Exposes: get_connection, init_db, reset_running_debates_to_paused
Tests: tests/test_debate_flow.py, tests/test_compaction.py, tests/test_auth_and_roles.py
Invariants: Every SQLite connection enables WAL journal mode and foreign key constraints; migrations are idempotent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    theme TEXT NOT NULL DEFAULT 'obsidian',
    show_thinking_default INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    api_protocol TEXT NOT NULL DEFAULT 'http',
    api_host TEXT NOT NULL DEFAULT '127.0.0.1',
    api_port INTEGER NOT NULL DEFAULT 8442,
    api_key_encrypted TEXT NOT NULL DEFAULT '',
    open_registration INTEGER NOT NULL DEFAULT 1,
    suite_application TEXT NOT NULL DEFAULT 'ai-debate',
    default_priority TEXT NULL,
    default_sensitivity TEXT NULL,
    default_node TEXT NULL,
    log_off INTEGER NOT NULL DEFAULT 0,
    no_wake INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_prompts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    style_category TEXT NOT NULL DEFAULT 'custom',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_custom_prompts_user ON custom_prompts(user_id, updated_at);

CREATE TABLE IF NOT EXISTS debates (
    id TEXT PRIMARY KEY,
    user_id TEXT NULL,
    title TEXT NOT NULL,
    scene_prompt TEXT NOT NULL DEFAULT '',
    first_speaker TEXT NOT NULL DEFAULT 'A',
    resolved_first_speaker TEXT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    turn_limit INTEGER NULL,
    time_limit_seconds INTEGER NULL,
    elapsed_active_seconds REAL NOT NULL DEFAULT 0.0,
    auto_compact_threshold REAL NOT NULL DEFAULT 0.78,
    keep_recent_turns INTEGER NOT NULL DEFAULT 4,
    show_thinking INTEGER NOT NULL DEFAULT 1,
    model_a_config TEXT NOT NULL,
    model_b_config TEXT NOT NULL,
    model_a_ctx_loaded INTEGER NULL,
    model_a_ctx_guaranteed INTEGER NULL,
    model_a_ctx_max INTEGER NULL,
    model_b_ctx_loaded INTEGER NULL,
    model_b_ctx_guaranteed INTEGER NULL,
    model_b_ctx_max INTEGER NULL,
    last_error TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_debates_user_updated ON debates(user_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    debate_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    sender TEXT NOT NULL,
    model_id TEXT NULL,
    answered_model_id TEXT NULL,
    answered_node TEXT NULL,
    content TEXT NOT NULL DEFAULT '',
    thinking TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NULL,
    completion_tokens INTEGER NULL,
    loaded_context_tokens INTEGER NULL,
    duration_ms INTEGER NULL,
    tokens_per_sec REAL NULL,
    request_id TEXT NULL,
    loadcoach_request_id TEXT NULL,
    finish_reason TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (debate_id) REFERENCES debates(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_debate_turn
    ON messages(debate_id, turn_index, created_at);

CREATE TABLE IF NOT EXISTS compactions (
    id TEXT PRIMARY KEY,
    debate_id TEXT NOT NULL,
    through_message_id TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    summarizer_model_id TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (debate_id) REFERENCES debates(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_compactions_debate_created
    ON compactions(debate_id, created_at);
"""


@contextmanager
def get_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection with WAL mode, Row factory, and transaction management.

    Parameters:
        db_path: Filesystem path to the SQLite database file.
    Returns:
        Context manager yielding an open sqlite3.Connection.
    Side effects:
        Creates parent directories if missing and commits/rolls back on exit.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_debates_columns(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(debates)").fetchall()}
    if "auto_compact_threshold" not in cols:
        conn.execute(
            "ALTER TABLE debates ADD COLUMN auto_compact_threshold REAL NOT NULL DEFAULT 0.78"
        )
    if "keep_recent_turns" not in cols:
        conn.execute("ALTER TABLE debates ADD COLUMN keep_recent_turns INTEGER NOT NULL DEFAULT 4")


def init_db(db_path: Path) -> None:
    """Initialize SQLite schema tables, indexes, and idempotent column migrations.

    Parameters:
        db_path: Filesystem path to the SQLite database file.
    Returns:
        None.
    Side effects:
        Creates database file, tables, and missing columns on disk.
    """
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_debates_columns(conn)


def reset_running_debates_to_paused(db_path: Path) -> int:
    """Transition any debates left in 'running' state after a crash/restart cleanly to 'paused'.

    Parameters:
        db_path: Filesystem path to the SQLite database file.
    Returns:
        Number of debate rows updated.
    Side effects:
        Updates debates table in SQLite.
    """
    now = datetime.now(UTC).isoformat()
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE debates SET status = 'paused', updated_at = ? WHERE status = 'running'",
            (now,),
        )
        return cur.rowcount
