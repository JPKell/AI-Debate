"""
Purpose: FastAPI router exposing proxied openWeightGym chat models, debate presets, and endpoint health.
Role: router
Patterns: thin-router, structured-errors
Depends on: fastapi, app.routers.auth, app.services.llm_client, app.services.prompt_builder
Exposes: router
Tests: tests/test_llm_client.py, tests/test_auth_and_roles.py
Invariants: Never returns raw API keys; requires authenticated session for model discovery.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.routers.auth import CurrentUser
from app.services.prompt_builder import CONVICTION_LEVELS, STYLE_PRESETS

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/health")
async def get_health(request: Request) -> dict[str, Any]:
    """Return service health and busy status (without exposing secrets or private debate IDs)."""
    settings = request.app.state.settings_repo.get_public_settings()
    manager = request.app.state.debate_manager
    return {
        "status": "ok",
        "owg_endpoint": f"{settings.api_protocol}://{settings.api_host}:{settings.api_port}",
        "api_key_configured": settings.api_key_configured,
        "busy": manager.active_debate_id is not None,
    }


@router.get("/models")
async def list_models(
    request: Request,
    _user: CurrentUser,
) -> dict[str, Any]:
    """Proxy `/v1/models` from openWeightGym filtered to chat-capable models."""
    llm_client = request.app.state.llm_client
    models = await llm_client.list_chat_models()
    return {
        "endpoint": llm_client.base_url,
        "models": [m.model_dump() for m in models],
    }


@router.get("/presets")
async def list_presets() -> dict[str, Any]:
    """Return built-in debate styles and conviction levels."""
    return {
        "styles": STYLE_PRESETS,
        "convictions": CONVICTION_LEVELS,
    }
