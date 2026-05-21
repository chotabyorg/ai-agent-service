from mcp_clients.manager import MCPManager


GOOGLE_CREDENTIALS = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "redirect_uri": "http://localhost/oauth2/callback",
    "refresh_token": "refresh-token",
    "scopes": "https://www.googleapis.com/auth/spreadsheets",
    "token_storage_mode": "local",
}

GOOGLE_STAGE_2_CREDENTIALS = {
    **GOOGLE_CREDENTIALS,
    "scopes": (
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/calendar"
    ),
}

GOOGLE_STAGE_3_CREDENTIALS = {
    **GOOGLE_CREDENTIALS,
    "scopes": (
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose"
    ),
}


def test_build_env_injects_google_workspace_credentials() -> None:
    manager = MCPManager("google", GOOGLE_CREDENTIALS)

    env = manager._build_env()

    assert env["GOOGLE_CLIENT_ID"] == "client-id"
    assert env["GOOGLE_CLIENT_SECRET"] == "client-secret"
    assert env["GOOGLE_REDIRECT_URI"] == "http://localhost/oauth2/callback"
    assert env["GOOGLE_REFRESH_TOKEN"] == "refresh-token"
    assert env["GOOGLE_SCOPES"] == "https://www.googleapis.com/auth/spreadsheets"


async def test_google_workspace_mcp_subprocess_lists_stage_1_tools() -> None:
    async with MCPManager("google", GOOGLE_STAGE_3_CREDENTIALS) as manager:
        tool_names = {tool.name for tool in manager._tools}
        metadata = await manager.get_tools_metadata()

    assert "google_sheets_upsert_lead" in tool_names
    assert "google_sheets_update_lead_status" in tool_names
    assert "google_calendar_create_followup" in tool_names
    assert "gmail_create_draft" in tool_names
    assert metadata["google_sheets_upsert_lead"] == {"category": "google_sheets", "tier": "core"}
