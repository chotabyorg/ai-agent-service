import json

import pytest

from mcp_clients.manager import MCPManager


GOOGLE_CALENDAR_CREDENTIALS = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "redirect_uri": "http://localhost/oauth2/callback",
    "refresh_token": "refresh-token",
    "scopes": "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/calendar",
    "token_storage_mode": "local",
}


def _tool_payload(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_google_workspace_mcp_subprocess_lists_calendar_tools() -> None:
    async with MCPManager("google", GOOGLE_CALENDAR_CREDENTIALS) as manager:
        tool_names = {tool.name for tool in manager._tools}
        metadata = await manager.get_tools_metadata()

    assert "google_calendar_create_event" in tool_names
    assert "google_calendar_create_followup" in tool_names
    assert metadata["google_calendar_create_event"] == {"category": "google_calendar", "tier": "core"}


@pytest.mark.asyncio
async def test_calendar_create_event_dry_run_does_not_require_google_call() -> None:
    async with MCPManager("google", GOOGLE_CALENDAR_CREDENTIALS) as manager:
        result = await manager.call_tool(
            "google_calendar_create_event",
            {
                "title": "Discovery call",
                "start": "2026-06-01T12:00:00Z",
                "end": "2026-06-01T12:30:00Z",
                "attendees": [{"email": "lead@example.com"}],
                "dryRun": True,
            },
        )

    payload = _tool_payload(result)
    assert payload["ok"] is True
    assert payload["action"] == "calendar_event_create_dry_run"
    assert payload["dryRun"] is True
    assert payload["data"]["event"]["attendees"] == [{"email": "lead@example.com", "optional": False}]


@pytest.mark.asyncio
async def test_calendar_delete_requires_destructive_tools_enabled() -> None:
    async with MCPManager("google", GOOGLE_CALENDAR_CREDENTIALS) as manager:
        result = await manager.call_tool(
            "google_calendar_delete_event",
            {
                "calendarId": "primary",
                "eventId": "event-1",
                "requiresConfirmation": True,
            },
        )

    payload = _tool_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DESTRUCTIVE_TOOLS_DISABLED"
