"""
Purpose: Define typed domain exception classes for structured API and upstream LLM error reporting.
Role: schema
Patterns: structured-errors
Depends on: typing
Exposes: AppError, UnauthorizedError, ForbiddenError, ResourceNotFoundError, DebateNotFoundError, DebateBusyError, DebateStateError, UpstreamLLMError
Tests: tests/test_llm_client.py, tests/test_debate_flow.py, tests/test_auth_and_roles.py
Invariants: Error payloads never include raw API keys, session tokens, or internal stack traces.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base domain error carrying a machine-readable code, user message, and HTTP status.

    Parameters:
        code: Stable machine-readable error identifier.
        message: Safe user-facing error description.
        status_code: HTTP status code for router responses.
        details: Optional non-sensitive structured metadata.
    Returns:
        AppError exception instance.
    Side effects:
        None.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        """Serialize error into a consistent JSON response envelope."""
        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class UnauthorizedError(AppError):
    """Raised when authentication is missing, invalid, or expired."""

    def __init__(self, message: str = "Authentication required.") -> None:
        super().__init__(code="unauthorized", message=message, status_code=401)


class ForbiddenError(AppError):
    """Raised when an authenticated user lacks required permissions or role."""

    def __init__(self, message: str = "You do not have permission to perform this action.") -> None:
        super().__init__(code="forbidden", message=message, status_code=403)


class ResourceNotFoundError(AppError):
    """Raised when a requested resource does not exist or is not owned by the caller."""

    def __init__(self, message: str = "Resource not found.") -> None:
        super().__init__(code="not_found", message=message, status_code=404)


class DebateNotFoundError(AppError):
    """Raised when a requested debate ID does not exist."""

    def __init__(self, debate_id: str) -> None:
        super().__init__(
            code="debate_not_found",
            message=f"Debate '{debate_id}' was not found.",
            status_code=404,
        )


class DebateBusyError(AppError):
    """Raised when attempting to start or resume a debate while another is active."""

    def __init__(self, active_debate_id: str | None = None) -> None:
        details = {"active_debate_id": active_debate_id} if active_debate_id else {}
        super().__init__(
            code="debate_busy",
            message="A debate is currently running on the server. Please wait until it pauses or finishes.",
            status_code=409,
            details=details,
        )


class DebateStateError(AppError):
    """Raised when a debate action is invalid for the debate's current lifecycle status."""

    def __init__(self, message: str, code: str = "invalid_debate_state") -> None:
        super().__init__(code=code, message=message, status_code=409)


class UpstreamLLMError(AppError):
    """Structured error returned from or raised while communicating with openWeightGym.

    Parameters:
        message: Safe user-facing error explanation.
        upstream_status: HTTP status from openWeightGym (0 for network/disconnect errors).
        upstream_code: Machine-readable error.code from upstream JSON if present.
        loaded_context: Loaded window reported by context_too_small errors.
        max_context: Maximum window reported by context_too_small errors.
        retry_after: Advisory Retry-After seconds from 429/503 responses.
        turnstile: X-Turnstile header value (e.g., compute-off or busy).
        request_id: X-Request-ID correlation header from openWeightGym.
        loadcoach_request_id: X-LoadCoach-Request-ID correlation header if logged.
    Returns:
        UpstreamLLMError exception instance.
    Side effects:
        None.
    """

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int = 502,
        upstream_code: str = "upstream_error",
        loaded_context: int | None = None,
        max_context: int | None = None,
        retry_after: int | None = None,
        turnstile: str | None = None,
        request_id: str | None = None,
        loadcoach_request_id: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "upstream_status": upstream_status,
            "upstream_code": upstream_code,
        }
        if loaded_context is not None:
            details["loaded_context"] = loaded_context
        if max_context is not None:
            details["max_context"] = max_context
        if retry_after is not None:
            details["retry_after"] = retry_after
        if turnstile:
            details["turnstile"] = turnstile
        if request_id:
            details["request_id"] = request_id
        if loadcoach_request_id:
            details["loadcoach_request_id"] = loadcoach_request_id

        http_status = upstream_status if 400 <= upstream_status < 600 else 502
        super().__init__(
            code=upstream_code or "upstream_error",
            message=message,
            status_code=http_status,
            details=details,
        )
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code
        self.loaded_context = loaded_context
        self.max_context = max_context
        self.retry_after = retry_after
        self.turnstile = turnstile
        self.request_id = request_id
        self.loadcoach_request_id = loadcoach_request_id
