import json

import pytest
from pydantic import ValidationError

from mcp_clients.manager import MCPManager
from mcp_clients.servers.google_workspace.schemas.drive import DriveCreateTextFileInput
from mcp_clients.servers.google_workspace.services.drive_service import build_drive_search_query


GOOGLE_STAGE_4_CREDENTIALS = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "redirect_uri": "http://localhost/oauth2/callback",
    "refresh_token": "refresh-token",
    "scopes": (
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose "
        "https://www.googleapis.com/auth/drive.file "
        "https://www.googleapis.com/auth/drive.metadata.readonly "
        "https://www.googleapis.com/auth/contacts"
    ),
    "token_storage_mode": "local",
}


def _tool_payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_drive_create_text_file_restricts_mime_type() -> None:
    with pytest.raises(ValidationError):
        DriveCreateTextFileInput.model_validate({
            "name": "report.html",
            "content": "<html></html>",
            "mimeType": "text/html",
        })


def test_build_drive_search_query_escapes_values() -> None:
    query = build_drive_search_query("Alice's report", "text/plain")

    assert query == "trashed = false and name contains 'Alice\\'s report' and mimeType = 'text/plain'"


@pytest.mark.asyncio
async def test_google_workspace_mcp_subprocess_lists_drive_tools() -> None:
    async with MCPManager("google_workspace", GOOGLE_STAGE_4_CREDENTIALS) as manager:
        names = {tool.name for tool in manager._tools}
        metadata = await manager.get_tools_metadata()

    assert "google_drive_search_files" in names
    assert "google_drive_create_folder" in names
    assert "google_drive_create_text_file" in names
    assert metadata["google_drive_create_text_file"] == {"category": "google_drive", "tier": "core"}


@pytest.mark.asyncio
async def test_drive_create_text_file_dry_run_does_not_require_google_call() -> None:
    async with MCPManager("google_workspace", GOOGLE_STAGE_4_CREDENTIALS) as manager:
        result = await manager.call_tool(
            "google_drive_create_text_file",
            {
                "name": "lead-report.json",
                "content": "{\"ok\": true}",
                "mimeType": "application/json",
                "dryRun": True,
            },
        )

    payload = _tool_payload(result)
    assert payload["ok"] is True
    assert payload["action"] == "drive_text_file_create_dry_run"
    assert payload["data"]["file"]["mimeType"] == "application/json"


@pytest.mark.asyncio
async def test_drive_create_folder_dry_run_does_not_require_google_call() -> None:
    async with MCPManager("google_workspace", GOOGLE_STAGE_4_CREDENTIALS) as manager:
        result = await manager.call_tool(
            "google_drive_create_folder",
            {
                "name": "Lead Reports",
                "dryRun": True,
            },
        )

    payload = _tool_payload(result)
    assert payload["ok"] is True
    assert payload["action"] == "drive_folder_create_dry_run"
