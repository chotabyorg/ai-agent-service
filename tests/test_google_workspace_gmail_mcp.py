import json

import pytest

from mcp_clients.manager import MCPManager


GOOGLE_GMAIL_CREDENTIALS = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "redirect_uri": "http://localhost/oauth2/callback",
    "refresh_token": "refresh-token",
    "scopes": (
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose"
    ),
    "token_storage_mode": "local",
}


def _tool_payload(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_google_workspace_mcp_subprocess_lists_gmail_tools() -> None:
    async with MCPManager("google", GOOGLE_GMAIL_CREDENTIALS) as manager:
        tool_names = {tool.name for tool in manager._tools}
        metadata = await manager.get_tools_metadata()

    assert "gmail_search_emails" in tool_names
    assert "gmail_create_draft" in tool_names
    assert "gmail_send_draft" in tool_names
    assert metadata["gmail_send_draft"] == {"category": "gmail", "tier": "core"}


@pytest.mark.asyncio
async def test_gmail_create_draft_dry_run_does_not_require_google_call() -> None:
    async with MCPManager("google", GOOGLE_GMAIL_CREDENTIALS) as manager:
        result = await manager.call_tool(
            "gmail_create_draft",
            {
                "to": ["lead@example.com"],
                "subject": "Proposal",
                "body": "Hello",
                "dryRun": True,
            },
        )

    payload = _tool_payload(result)
    assert payload["ok"] is True
    assert payload["action"] == "gmail_draft_create_dry_run"
    assert payload["dryRun"] is True
    assert payload["data"]["message"]["rawLength"] > 0


@pytest.mark.asyncio
async def test_gmail_send_draft_disabled_by_default() -> None:
    async with MCPManager("google", GOOGLE_GMAIL_CREDENTIALS) as manager:
        result = await manager.call_tool(
            "gmail_send_draft",
            {
                "draftId": "draft-1",
                "requiresConfirmation": True,
            },
        )

    payload = _tool_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "GMAIL_SEND_DISABLED"
