"""
Purpose: FastAPI router and dependencies for user registration, login, logout, session cookies, and user preferences.
Role: router
Patterns: thin-router, structured-errors
Depends on: fastapi, app.errors, app.schemas, app.security
Exposes: SESSION_COOKIE_NAME, CurrentUser, AdminUser, get_optional_user, require_user, require_admin, router
Tests: tests/test_auth_and_roles.py
Invariants: Session cookies are HttpOnly and SameSite=Lax; first registered user becomes admin; password hashes are never exposed.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from app.errors import ForbiddenError, UnauthorizedError
from app.schemas import AuthCredentials, UserPreferencesUpdate, UserRecord
from app.security import sign_session_cookie, verify_session_cookie

SESSION_COOKIE_NAME = "ai_debate_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str, master_secret: str) -> None:
    signed = sign_session_cookie(token, master_secret)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def get_optional_user(request: Request) -> UserRecord | None:
    """Resolve the signed session cookie to an active UserRecord, or None if unauthenticated."""
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_cookie:
        return None
    token = verify_session_cookie(raw_cookie, request.app.state.master_secret)
    if not token:
        return None
    return request.app.state.users_repo.get_user_by_session_token(token)


def require_user(request: Request) -> UserRecord:
    """FastAPI dependency enforcing an authenticated, active user session."""
    user = get_optional_user(request)
    if user is None:
        raise UnauthorizedError("Please sign in to continue.")
    return user


CurrentUser = Annotated[UserRecord, Depends(require_user)]


def require_admin(user: CurrentUser) -> UserRecord:
    """FastAPI dependency enforcing the `admin` role."""
    if user.role != "admin":
        raise ForbiddenError("Administrator privileges are required.")
    return user


AdminUser = Annotated[UserRecord, Depends(require_admin)]


@router.get("/status")
async def auth_status(request: Request) -> dict[str, Any]:
    """Return bootstrap state (has_users), open_registration flag, and currently signed-in user if any."""
    users_repo = request.app.state.users_repo
    settings = request.app.state.settings_repo.get_public_settings()
    user = get_optional_user(request)
    return {
        "has_users": users_repo.count_users() > 0,
        "open_registration": settings.open_registration,
        "user": user.model_dump() if user else None,
    }


@router.post("/register", response_model=UserRecord, status_code=201)
async def register(
    request: Request,
    response: Response,
    payload: AuthCredentials,
) -> UserRecord:
    """Register a new account (first user automatically becomes admin) and set a signed session cookie."""
    users_repo = request.app.state.users_repo
    settings = request.app.state.settings_repo.get_public_settings()
    has_users = users_repo.count_users() > 0
    if has_users and not settings.open_registration:
        raise ForbiddenError("Self-registration is disabled by the administrator.")

    user = users_repo.create_user(username=payload.username, password=payload.password)
    token = users_repo.create_session(user.id)
    _set_session_cookie(response, token, request.app.state.master_secret)
    return user


@router.post("/login", response_model=UserRecord)
async def login(
    request: Request,
    response: Response,
    payload: AuthCredentials,
) -> UserRecord:
    """Authenticate an existing user and issue a signed HttpOnly session cookie."""
    users_repo = request.app.state.users_repo
    user = users_repo.authenticate(username=payload.username, password=payload.password)
    if user is None:
        raise UnauthorizedError("Invalid username or password.")
    token = users_repo.create_session(user.id)
    _set_session_cookie(response, token, request.app.state.master_secret)
    return user


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    """Revoke current session and clear session cookie."""
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_cookie:
        token = verify_session_cookie(raw_cookie, request.app.state.master_secret)
        if token:
            request.app.state.users_repo.delete_session(token)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserRecord)
async def get_me(user: CurrentUser) -> UserRecord:
    """Return the currently authenticated user's profile."""
    return user


@router.patch("/preferences", response_model=UserRecord)
async def update_preferences(
    request: Request,
    patch: UserPreferencesUpdate,
    user: CurrentUser,
) -> UserRecord:
    """Persist per-user theme and thinking display preferences."""
    return request.app.state.users_repo.update_preferences(user.id, patch)
