"""
Purpose: FastAPI application factory, lifespan wiring, exception mapping, and authenticated frontend routes.
Role: router
Patterns: thin-router, structured-errors
Depends on: fastapi, contextlib, pathlib, app.config, app.errors, app.security, app.repositories, app.services, app.routers
Exposes: create_app, app
Tests: tests/test_debate_flow.py, tests/test_auth_and_roles.py
Invariants: Initializes SQLite WAL schema, seeds encrypted settings from .env if absent, resets stale running debates to paused on startup, and shuts down cleanly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, load_config
from app.errors import AppError
from app.repositories.db import init_db, reset_running_debates_to_paused
from app.repositories.debates_repo import DebatesRepository
from app.repositories.prompts_repo import PromptsRepository
from app.repositories.settings_repo import SettingsRepository
from app.repositories.users_repo import UsersRepository
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from app.routers import debates as debates_router
from app.routers import models as models_router
from app.routers import prompts as prompts_router
from app.routers.auth import get_optional_user
from app.security import load_or_create_secret_key
from app.services.compaction import CompactionService
from app.services.debate_manager import DebateManager
from app.services.llm_client import OpenWeightGymClient

APP_DIR = Path(__file__).resolve().parent


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create and wire the FastAPI application instance.

    Parameters:
        config: Optional AppConfig override (defaults to loading from `.env` and environment).
    Returns:
        Configured FastAPI application.
    Side effects:
        Mounts static files and Jinja2 templates; registers startup/shutdown lifespan hooks.
    """
    cfg = config or load_config()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        init_db(cfg.db_path)
        reset_running_debates_to_paused(cfg.db_path)

        master_secret = load_or_create_secret_key(cfg.data_dir, cfg.app_secret_key)
        users_repo = UsersRepository(cfg.db_path)
        settings_repo = SettingsRepository(cfg.db_path, master_secret=master_secret)
        settings_repo.seed_from_config_if_empty(cfg)
        prompts_repo = PromptsRepository(cfg.db_path)
        debates_repo = DebatesRepository(cfg.db_path)

        runtime = settings_repo.get_runtime_settings()
        llm_client = OpenWeightGymClient(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            suite_application=runtime.suite_application,
            default_priority=runtime.default_priority,
            default_sensitivity=runtime.default_sensitivity,
            default_node=runtime.default_node,
            log_off=runtime.log_off,
            no_wake=runtime.no_wake,
        )
        compaction_service = CompactionService(repo=debates_repo, llm_client=llm_client)
        manager = DebateManager(
            repo=debates_repo,
            llm_client=llm_client,
            compaction_service=compaction_service,
        )

        application.state.config = cfg
        application.state.master_secret = master_secret
        application.state.users_repo = users_repo
        application.state.settings_repo = settings_repo
        application.state.prompts_repo = prompts_repo
        application.state.debates_repo = debates_repo
        application.state.llm_client = llm_client
        application.state.compaction_service = compaction_service
        application.state.debate_manager = manager
        try:
            yield
        finally:
            await manager.shutdown()
            await llm_client.aclose()

    application = FastAPI(title="AI-Debate", version="0.2.0", lifespan=lifespan)

    @application.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    application.include_router(auth_router.router)
    application.include_router(admin_router.router)
    application.include_router(prompts_router.router)
    application.include_router(models_router.router)
    application.include_router(debates_router.router)

    static_dir = APP_DIR / "static"
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    if static_dir.exists():
        application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @application.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        user = get_optional_user(request)
        if user is not None:
            return RedirectResponse(url="/", status_code=302)
        return templates.TemplateResponse(request=request, name="login.html", context={})

    @application.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request) -> Response:
        user = get_optional_user(request)
        if user is None:
            return RedirectResponse(url="/login", status_code=302)
        if user.role != "admin":
            return RedirectResponse(url="/", status_code=302)
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={"user": user},
        )

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        user = get_optional_user(request)
        if user is None:
            return RedirectResponse(url="/login", status_code=302)
        rt = request.app.state.settings_repo.get_runtime_settings()
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context={
                "owg_endpoint": rt.base_url,
                "user": user,
            },
        )

    return application


app = create_app()
