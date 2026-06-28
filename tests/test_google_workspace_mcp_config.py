import pytest

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

GOOGLE_STAGE_4_CREDENTIALS = {
    **GOOGLE_CREDENTIALS,
    "scopes": (
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose "
        "https://www.googleapis.com/auth/drive.file "
        "https://www.googleapis.com/auth/drive.metadata.readonly "
        "https://www.googleapis.com/auth/contacts"
    ),
}


def test_build_env_injects_google_workspace_credentials() -> None:
    manager = MCPManager("google_workspace", GOOGLE_CREDENTIALS)

    env = manager._build_env()

    assert env["GOOGLE_CLIENT_ID"] == "client-id"
    assert env["GOOGLE_CLIENT_SECRET"] == "client-secret"
    assert env["GOOGLE_REDIRECT_URI"] == "http://localhost/oauth2/callback"
    assert env["GOOGLE_REFRESH_TOKEN"] == "refresh-token"
    assert env["GOOGLE_SCOPES"] == "https://www.googleapis.com/auth/spreadsheets"
    assert env["GOOGLE_TOKEN_STORAGE_MODE"] == "local"


async def test_google_workspace_mcp_subprocess_lists_stage_1_tools() -> None:
    async with MCPManager("google_workspace", GOOGLE_STAGE_4_CREDENTIALS) as manager:
        tool_names = {tool.name for tool in manager._tools}
        metadata = await manager.get_tools_metadata()

    assert "google_sheets_upsert_lead" in tool_names
    assert "google_sheets_update_lead_status" in tool_names
    assert "google_calendar_create_followup" in tool_names
    assert "gmail_create_draft" in tool_names
    assert "google_drive_create_text_file" in tool_names
    assert "google_contacts_upsert_contact" in tool_names
    assert metadata["google_sheets_upsert_lead"] == {"category": "google_sheets", "tier": "extended"}


async def test_google_workspace_mcp_exposes_only_sheets_tools_for_sheets_readonly_scope() -> None:
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": "https://www.googleapis.com/auth/spreadsheets.readonly",
    }

    async with MCPManager("google_workspace", credentials) as manager:
        tool_names = {tool.name for tool in manager._tools}

    assert tool_names
    assert all(name.startswith("google_sheets_") for name in tool_names)


async def test_google_workspace_mcp_exposes_only_calendar_tools_for_calendar_readonly_scope() -> None:
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": "https://www.googleapis.com/auth/calendar.readonly",
    }

    async with MCPManager("google_workspace", credentials) as manager:
        tool_names = {tool.name for tool in manager._tools}

    assert tool_names
    assert all(name.startswith("google_calendar_") for name in tool_names)


async def test_google_workspace_mcp_exposes_only_contacts_tools_for_contacts_readonly_scope() -> None:
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": "https://www.googleapis.com/auth/contacts.readonly",
    }

    async with MCPManager("google_workspace", credentials) as manager:
        tool_names = {tool.name for tool in manager._tools}

    assert tool_names
    assert all(name.startswith("google_contacts_") for name in tool_names)


async def test_google_workspace_mcp_exposes_only_gmail_tools_for_gmail_readonly_scope() -> None:
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": "https://www.googleapis.com/auth/gmail.readonly",
    }

    async with MCPManager("google_workspace", credentials) as manager:
        tool_names = {tool.name for tool in manager._tools}

    assert tool_names
    assert all(name.startswith("gmail_") for name in tool_names)


async def test_google_workspace_mcp_exposes_only_drive_tools_for_drive_readonly_scope() -> None:
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": "https://www.googleapis.com/auth/drive.metadata.readonly",
    }

    async with MCPManager("google_workspace", credentials) as manager:
        tool_names = {tool.name for tool in manager._tools}

    assert tool_names
    assert all(name.startswith("google_drive_") for name in tool_names)


async def test_google_workspace_default_core_tools_respect_configured_scopes() -> None:
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": "https://www.googleapis.com/auth/spreadsheets.readonly",
    }

    async with MCPManager("google_workspace", credentials) as manager:
        metadata = await manager.get_tools_metadata()
        default_tool_names = {
            tool.name
            for tool in manager._tools
            if metadata.get(tool.name, {}).get("tier", "core") == "core"
        }

    assert default_tool_names == {
        "google_sheets_get_spreadsheet",
        "google_sheets_list_tabs",
        "google_sheets_read_range",
        "google_sheets_search_rows",
    }


@pytest.mark.parametrize(
    "scope",
    [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/drive.file",
    ],
)
async def test_google_workspace_default_core_tools_exclude_write_only_scopes(scope: str) -> None:
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "scopes": scope,
    }

    async with MCPManager("google_workspace", credentials) as manager:
        metadata = await manager.get_tools_metadata()
        default_tool_names = {
            tool.name
            for tool in manager._tools
            if metadata.get(tool.name, {}).get("tier", "core") == "core"
        }

    assert default_tool_names == set()


def test_build_env_missing_required_field_raises() -> None:
    minimal = {"client_id": "test", "client_secret": "secret"}
    manager = MCPManager("google_workspace", minimal)
    with pytest.raises(ValueError, match="Missing credential 'refresh_token'"):
        manager._build_env()


def test_build_env_missing_optional_field_is_ok() -> None:
    minimal = {
        "client_id": "test",
        "client_secret": "secret",
        "refresh_token": "token",
    }
    manager = MCPManager("google_workspace", minimal)
    env = manager._build_env()
    assert env["GOOGLE_CLIENT_ID"] == "test"
    assert "GOOGLE_SCOPES" not in env
    assert "GOOGLE_REDIRECT_URI" not in env
