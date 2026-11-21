"""
Purpose: SQLite repository for singleton app_settings with Fernet-encrypted openWeightGym API key persistence.
Role: repository
Patterns: repository-pattern
Depends on: dataclasses, datetime, pathlib, app.config, app.repositories.db, app.schemas, app.security
Exposes: RuntimeSettings, SettingsRepository
Tests: tests/test_auth_and_roles.py
Invariants: API keys are always stored Fernet-encrypted in SQLite and never exposed in AdminSettingsResponse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.config import AppConfig
from app.repositories.db import get_connection
from app.schemas import AdminSettingsResponse, AdminSettingsUpdate
from app.security import decrypt_secret, encrypt_secret


@dataclass(frozen=True)
class RuntimeSettings:
    """Decrypted runtime settings consumed internally by OpenWeightGymClient."""

    api_protocol: Literal["http", "https"] = "http"
    api_host: str = "127.0.0.1"
    api_port: int = 8442
    api_key: str = field(default="", repr=False)
    open_registration: bool = True
    suite_application: str = "ai-debate"
    default_priority: str | None = None
    default_sensitivity: str | None = None
    default_node: str | None = None
    log_off: bool = False
    no_wake: bool = False

    @property
    def base_url(self) -> str:
        """Return normalized openWeightGym base URL."""
        return f"{self.api_protocol}://{self.api_host}:{self.api_port}"


class SettingsRepository:
    """Singleton repository for server configuration and encrypted API key storage."""

    def __init__(self, db_path: Path, master_secret: str) -> None:
        self.db_path = db_path
        self._master_secret = master_secret

    def seed_from_config_if_empty(self, config: AppConfig) -> None:
        """Initialize the singleton app_settings row from .env/AppConfig if not yet populated.

        Parameters:
            config: Loaded AppConfig carrying defaults from `.env`.
        Returns:
            None.
        Side effects:
            Inserts singleton row `id=1` into `app_settings` if absent.
        """
        now = datetime.now(UTC).isoformat()
        encrypted_key = encrypt_secret(config.owg_api_key, self._master_secret) if config.owg_api_key else ""
        with get_connection(self.db_path) as conn:
            existing = conn.execute("SELECT id FROM app_settings WHERE id = 1").fetchone()
            if existing is not None:
                return
            conn.execute(
                """
                INSERT INTO app_settings (
                    id, api_protocol, api_host, api_port, api_key_encrypted,
                    open_registration, suite_application, default_priority,
                    default_sensitivity, default_node, log_off, no_wake, updated_at
                ) VALUES (1, ?, ?, ?, ?, 1, ?, NULL, NULL, NULL, 0, 0, ?)
                """,
                (
                    config.owg_protocol,
                    config.owg_host,
                    config.owg_port,
                    encrypted_key,
                    config.suite_application,
                    now,
                ),
            )

    def get_runtime_settings(self) -> RuntimeSettings:
        """Read and decrypt runtime openWeightGym settings for internal service use."""
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
        if row is None:
            return RuntimeSettings()
        decrypted_key = (
            decrypt_secret(row["api_key_encrypted"], self._master_secret)
            if row["api_key_encrypted"]
            else ""
        )
        proto: Literal["http", "https"] = "https" if row["api_protocol"] == "https" else "http"
        return RuntimeSettings(
            api_protocol=proto,
            api_host=row["api_host"],
            api_port=int(row["api_port"]),
            api_key=decrypted_key,
            open_registration=bool(row["open_registration"]),
            suite_application=row["suite_application"],
            default_priority=row["default_priority"],
            default_sensitivity=row["default_sensitivity"],
            default_node=row["default_node"],
            log_off=bool(row["log_off"]),
            no_wake=bool(row["no_wake"]),
        )

    def get_public_settings(self) -> AdminSettingsResponse:
        """Return admin-visible settings DTO with API key redacted to a boolean flag."""
        rt = self.get_runtime_settings()
        return AdminSettingsResponse(
            api_protocol=rt.api_protocol,
            api_host=rt.api_host,
            api_port=rt.api_port,
            api_key_configured=bool(rt.api_key),
            open_registration=rt.open_registration,
            suite_application=rt.suite_application,
            default_priority=rt.default_priority,  # type: ignore[arg-type]
            default_sensitivity=rt.default_sensitivity,  # type: ignore[arg-type]
            default_node=rt.default_node,
            log_off=rt.log_off,
            no_wake=rt.no_wake,
        )

    def update_settings(self, patch: AdminSettingsUpdate) -> AdminSettingsResponse:
        """Update singleton app_settings, encrypting any newly provided API key at rest."""
        current = self.get_runtime_settings()
        fields = patch.model_fields_set

        api_protocol = patch.api_protocol if "api_protocol" in fields and patch.api_protocol else current.api_protocol
        api_host = patch.api_host.strip() if "api_host" in fields and patch.api_host else current.api_host
        api_port = patch.api_port if "api_port" in fields and patch.api_port is not None else current.api_port
        open_reg = (
            patch.open_registration
            if "open_registration" in fields and patch.open_registration is not None
            else current.open_registration
        )
        suite_app = (
            patch.suite_application.strip()
            if "suite_application" in fields and patch.suite_application
            else current.suite_application
        )
        if "default_priority" in fields:
            default_priority = patch.default_priority or None
        else:
            default_priority = current.default_priority

        if "default_sensitivity" in fields:
            default_sensitivity = patch.default_sensitivity or None
        else:
            default_sensitivity = current.default_sensitivity

        if "default_node" in fields:
            default_node = patch.default_node.strip() if patch.default_node and patch.default_node.strip() else None
        else:
            default_node = current.default_node

        log_off = patch.log_off if "log_off" in fields and patch.log_off is not None else current.log_off
        no_wake = patch.no_wake if "no_wake" in fields and patch.no_wake is not None else current.no_wake

        if patch.clear_api_key:
            new_key = ""
        elif "api_key" in fields and patch.api_key is not None and patch.api_key.strip():
            new_key = patch.api_key.strip()
        else:
            new_key = current.api_key

        encrypted_key = encrypt_secret(new_key, self._master_secret) if new_key else ""
        now = datetime.now(UTC).isoformat()

        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_settings (
                    id, api_protocol, api_host, api_port, api_key_encrypted,
                    open_registration, suite_application, default_priority,
                    default_sensitivity, default_node, log_off, no_wake, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    api_protocol = excluded.api_protocol,
                    api_host = excluded.api_host,
                    api_port = excluded.api_port,
                    api_key_encrypted = excluded.api_key_encrypted,
                    open_registration = excluded.open_registration,
                    suite_application = excluded.suite_application,
                    default_priority = excluded.default_priority,
                    default_sensitivity = excluded.default_sensitivity,
                    default_node = excluded.default_node,
                    log_off = excluded.log_off,
                    no_wake = excluded.no_wake,
                    updated_at = excluded.updated_at
                """,
                (
                    api_protocol,
                    api_host,
                    api_port,
                    encrypted_key,
                    1 if open_reg else 0,
                    suite_app,
                    default_priority,
                    default_sensitivity,
                    default_node,
                    1 if log_off else 0,
                    1 if no_wake else 0,
                    now,
                ),
            )
        return self.get_public_settings()
