"""
Purpose: Admin-only FastAPI router for openWeightGym connection settings, Test Connection, user management, and global force-stop.
Role: router
Patterns: thin-router, structured-errors
Depends on: fastapi, app.routers.auth, app.schemas
Exposes: router
Tests: tests/test_auth_and_roles.py
Invariants: Every route enforces admin role; API keys are never returned in plaintext; force-stop never exposes private debate transcripts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.routers.auth import require_admin
from app.schemas import (
    AdminSettingsResponse,
    AdminSettingsUpdate,
    AdminUserCreate,
    AdminUserUpdate,
    TestConnectionRequest,
    UserRecord,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/settings", response_model=AdminSettingsResponse)
async def get_settings(request: Request) -> AdminSettingsResponse:
    """Return server settings with API key redacted to `api_key_configured`."""
    return request.app.state.settings_repo.get_public_settings()


@router.patch("/settings", response_model=AdminSettingsResponse)
async def update_settings(request: Request, patch: AdminSettingsUpdate) -> AdminSettingsResponse:
    """Update encrypted server settings in SQLite and hot-reload OpenWeightGymClient."""
    settings_repo = request.app.state.settings_repo
    updated = settings_repo.update_settings(patch)
    runtime = settings_repo.get_runtime_settings()
    await request.app.state.llm_client.configure(
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        suite_application=runtime.suite_application,
        default_priority=runtime.default_priority,
        default_sensitivity=runtime.default_sensitivity,
        default_node=runtime.default_node,
        log_off=runtime.log_off,
        no_wake=runtime.no_wake,
    )
    return updated


@router.post("/test-connection")
async def test_connection(
    request: Request,
    payload: TestConnectionRequest,
) -> dict[str, Any]:
    """Verify openWeightGym connectivity using candidate or currently saved settings."""
    runtime = request.app.state.settings_repo.get_runtime_settings()
    proto = payload.api_protocol or runtime.api_protocol
    host = (payload.api_host or runtime.api_host).strip()
    port = payload.api_port or runtime.api_port
    api_key = (
        payload.api_key.strip()
        if payload.api_key is not None and payload.api_key.strip()
        else runtime.api_key
    )
    suite_app = payload.suite_application or runtime.suite_application

    return await request.app.state.llm_client.test_connection(
        base_url=f"{proto}://{host}:{port}",
        api_key=api_key,
        suite_application=suite_app,
    )


@router.get("/users", response_model=list[UserRecord])
async def list_users(request: Request) -> list[UserRecord]:
    """List all user accounts."""
    return request.app.state.users_repo.list_users()


@router.post("/users", response_model=UserRecord, status_code=201)
async def create_user(request: Request, payload: AdminUserCreate) -> UserRecord:
    """Create a user account explicitly from the Admin panel."""
    return request.app.state.users_repo.create_user(
        username=payload.username,
        password=payload.password,
        role=payload.role,
    )


@router.patch("/users/{user_id}", response_model=UserRecord)
async def update_user(
    request: Request,
    user_id: str,
    patch: AdminUserUpdate,
) -> UserRecord:
    """Update a user's role, active flag, or password."""
    return request.app.state.users_repo.admin_update_user(user_id, patch)


@router.get("/status")
async def server_busy_status(request: Request) -> dict[str, Any]:
    """Report whether any debate is currently active without exposing private debate content."""
    manager = request.app.state.debate_manager
    return {
        "busy": manager.active_debate_id is not None,
    }


@router.post("/force-stop")
async def force_stop_active_debate(request: Request) -> dict[str, bool]:
    """Force-stop any stuck active debate globally without viewing its transcript."""
    stopped = await request.app.state.debate_manager.force_stop_active()
    return {"stopped": stopped}
