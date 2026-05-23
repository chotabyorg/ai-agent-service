"""Google Drive MCP tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError
from mcp_clients.servers.google_workspace.config.settings import (
    DRIVE_FILE_SCOPE,
    DRIVE_METADATA_SCOPE,
    get_google_settings,
)
from mcp_clients.servers.google_workspace.google.drive_client import GoogleDriveClient
from mcp_clients.servers.google_workspace.schemas.drive import (
    DriveCreateFolderInput,
    DriveCreateTextFileInput,
    DriveSearchFilesInput,
)
from mcp_clients.servers.google_workspace.services.drive_service import (
    build_drive_search_query,
    file_resource,
)
from mcp_clients.servers.google_workspace.utils.responses import error_response, success_response


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        return error_response(
            "VALIDATION_ERROR",
            "Tool input validation failed.",
            details={"errors": json.loads(exc.json())},
        )
    if isinstance(exc, GoogleAuthError):
        return error_response(exc.code, str(exc), recoverable=exc.recoverable)
    if isinstance(exc, ValueError):
        return error_response("VALIDATION_ERROR", str(exc), recoverable=True)
    return error_response("GOOGLE_API_ERROR", "Google Drive request failed.", details={"type": exc.__class__.__name__})


def _get_drive_client() -> GoogleDriveClient:
    return GoogleDriveClient(get_google_settings())


def register_drive_tools(mcp: Any, tool: Callable[[str, str], Callable]) -> None:
    """Register Google Drive tools on a FastMCP server."""

    @tool("google_drive", "core")
    async def google_drive_search_files(
        query: str | None = None,
        mimeType: str | None = None,
        maxResults: int = 10,
    ) -> dict[str, Any]:
        """Search Drive files by name and optional MIME type."""

        try:
            payload = DriveSearchFilesInput.model_validate({
                "query": query,
                "mimeType": mimeType,
                "maxResults": maxResults,
            })
            drive_query = build_drive_search_query(payload.query, payload.mimeType)
            files = await _get_drive_client().search_files(
                query=drive_query,
                max_results=payload.maxResults,
                required_scopes=[DRIVE_METADATA_SCOPE],
            )
            return success_response(
                action="drive_files_searched",
                resource={"type": "google_drive_search"},
                summary=f"Found {len(files)} matching Drive files.",
                data={"query": drive_query, "files": files},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_drive", "core")
    async def google_drive_create_folder(
        name: str,
        parentFolderId: str | None = None,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Create a Google Drive folder. Honors dryRun for write safety."""

        try:
            payload = DriveCreateFolderInput.model_validate(locals())
            if payload.dryRun:
                return success_response(
                    action="drive_folder_create_dry_run",
                    resource={"type": "google_drive_folder"},
                    dry_run=True,
                    summary="Dry run: Drive folder creation was validated but not executed.",
                    data={"folder": {"name": payload.name, "parentFolderId": payload.parentFolderId}},
                )
            folder = await _get_drive_client().create_folder(
                name=payload.name,
                parent_folder_id=payload.parentFolderId,
                required_scopes=[DRIVE_FILE_SCOPE],
            )
            return success_response(
                action="drive_folder_created",
                resource=file_resource(folder),
                summary="Drive folder created.",
                data={"folder": folder},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_drive", "core")
    async def google_drive_create_text_file(
        name: str,
        content: str,
        mimeType: str = "text/plain",
        parentFolderId: str | None = None,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Create a small text, JSON, Markdown or CSV file in Google Drive."""

        try:
            payload = DriveCreateTextFileInput.model_validate(locals())
            if payload.dryRun:
                return success_response(
                    action="drive_text_file_create_dry_run",
                    resource={"type": "google_drive_file"},
                    dry_run=True,
                    summary="Dry run: Drive text file creation was validated but not executed.",
                    data={
                        "file": {
                            "name": payload.name,
                            "mimeType": payload.mimeType,
                            "size": len(payload.content.encode("utf-8")),
                            "parentFolderId": payload.parentFolderId,
                        }
                    },
                )
            file = await _get_drive_client().create_text_file(
                name=payload.name,
                content=payload.content,
                mime_type=payload.mimeType,
                parent_folder_id=payload.parentFolderId,
                required_scopes=[DRIVE_FILE_SCOPE],
            )
            return success_response(
                action="drive_text_file_created",
                resource=file_resource(file),
                summary="Drive text file created.",
                data={"file": file},
            )
        except Exception as exc:
            return _tool_error(exc)
