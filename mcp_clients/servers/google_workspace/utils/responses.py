"""Structured MCP tool responses."""

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
