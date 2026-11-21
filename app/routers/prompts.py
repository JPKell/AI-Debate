"""
Purpose: FastAPI router for built-in debate style presets and per-user saved custom prompts CRUD.
Role: router
Patterns: thin-router, structured-errors
Depends on: fastapi, app.routers.auth, app.schemas, app.services.prompt_builder
Exposes: router
Tests: tests/test_auth_and_roles.py
Invariants: All custom prompt mutations and listings are strictly scoped to the authenticated user.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.routers.auth import CurrentUser
from app.schemas import CustomPromptCreate, CustomPromptRecord, CustomPromptUpdate
from app.services.prompt_builder import CONVICTION_LEVELS, STYLE_PRESETS

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.get("")
async def list_prompts(
    request: Request,
    user: CurrentUser,
) -> dict[str, Any]:
    """Return built-in style/conviction presets alongside the current user's custom prompts."""
    custom = request.app.state.prompts_repo.list_prompts(user.id)
    return {
        "styles": STYLE_PRESETS,
        "convictions": CONVICTION_LEVELS,
        "custom_prompts": [p.model_dump() for p in custom],
    }


@router.post("", response_model=CustomPromptRecord, status_code=201)
async def create_prompt(
    request: Request,
    payload: CustomPromptCreate,
    user: CurrentUser,
) -> CustomPromptRecord:
    """Create a custom prompt owned by the current user."""
    return request.app.state.prompts_repo.create_prompt(user.id, payload)


@router.patch("/{prompt_id}", response_model=CustomPromptRecord)
async def update_prompt(
    request: Request,
    prompt_id: str,
    patch: CustomPromptUpdate,
    user: CurrentUser,
) -> CustomPromptRecord:
    """Update a custom prompt owned by the current user."""
    return request.app.state.prompts_repo.update_prompt(user.id, prompt_id, patch)


@router.delete("/{prompt_id}", status_code=204)
async def delete_prompt(
    request: Request,
    prompt_id: str,
    user: CurrentUser,
) -> None:
    """Delete a custom prompt owned by the current user."""
    request.app.state.prompts_repo.delete_prompt(user.id, prompt_id)
