"""Structured MCP tool responses."""

import json
from typing import Any


def success_response(
    *,
    action: str,
    summary: str,
    resource: dict[str, Any] | None = None,
    dry_run: bool = False,
    warnings: list[str] | None = None,
    data: Any | None = None,
) -> dict[str, Any]:
    """Build a consistent successful tool response."""

    response: dict[str, Any] = {
        "ok": True,
        "action": action,
        "resource": resource,
        "dryRun": dry_run,
        "summary": summary,
        "warnings": warnings or [],
    }
    if data is not None:
        response["data"] = data
    return response


def error_response(
    code: str,
    message: str,
    *,
    recoverable: bool = True,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a consistent safe error response."""

    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }
    if details:
        error["details"] = details
    return {"ok": False, "error": error}


def google_api_error_response(exc: Exception, service: str = "Google Workspace") -> dict[str, Any]:
    """Build a safe structured response for Google API/auth transport failures."""

    refresh_error_class = _optional_refresh_error_class()
    if refresh_error_class is not None and isinstance(exc, refresh_error_class):
        return error_response(
            "GOOGLE_AUTH_INVALID",
            "Google OAuth credentials are invalid or expired. Re-authorize Google OAuth.",
            recoverable=True,
            details={"type": exc.__class__.__name__},
        )

    status = _http_status(exc)
    reason = _http_error_reason(exc)
    if status == 401:
        return error_response(
            "GOOGLE_AUTH_INVALID",
            "Google OAuth credentials are invalid or expired. Re-authorize Google OAuth.",
            recoverable=True,
            details=_google_error_details(exc, status, reason),
        )
    if status == 403:
        return error_response(
            "GOOGLE_PERMISSION_DENIED",
            "Google OAuth scope or account permission is insufficient for this request.",
            recoverable=True,
            details=_google_error_details(exc, status, reason),
        )
    if status == 404:
        return error_response(
            "GOOGLE_RESOURCE_NOT_FOUND",
            "Requested Google resource was not found or is not accessible.",
            recoverable=True,
            details=_google_error_details(exc, status, reason),
        )
    if status == 429:
        return error_response(
            "GOOGLE_RATE_LIMITED",
            "Google API rate limit was reached. Retry later or reduce request volume.",
            recoverable=True,
            details=_google_error_details(exc, status, reason),
        )
    if status and status >= 500:
        return error_response(
            "GOOGLE_API_UNAVAILABLE",
            "Google API is temporarily unavailable. Retry later.",
            recoverable=True,
            details=_google_error_details(exc, status, reason),
        )

    return error_response(
        "GOOGLE_API_ERROR",
        f"{service} request failed.",
        details=_google_error_details(exc, status, reason),
    )


def _optional_refresh_error_class() -> type[Exception] | None:
    try:
        from google.auth.exceptions import RefreshError
    except ImportError:
        return None
    return RefreshError


def _http_status(exc: Exception) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status if isinstance(status, int) else None


def _http_error_reason(exc: Exception) -> str | None:
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not isinstance(content, str) or not content:
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    reason = error.get("status") or error.get("reason")
    return reason if isinstance(reason, str) and reason else None


def _google_error_details(exc: Exception, status: int | None, reason: str | None) -> dict[str, Any]:
    details: dict[str, Any] = {"type": exc.__class__.__name__}
    if status:
        details["status"] = status
    if reason:
        details["reason"] = reason
    return details
