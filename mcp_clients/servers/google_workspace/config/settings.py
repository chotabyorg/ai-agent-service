"""Environment-backed settings for the Google Workspace MCP server."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_EVENTS_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
CONTACTS_READONLY_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"

GOOGLE_PRODUCT_SCOPES: dict[str, frozenset[str]] = {
    "sheets": frozenset({SHEETS_SCOPE, SHEETS_READONLY_SCOPE}),
    "calendar": frozenset({
        CALENDAR_SCOPE,
        CALENDAR_READONLY_SCOPE,
        CALENDAR_EVENTS_SCOPE,
        CALENDAR_EVENTS_READONLY_SCOPE,
    }),
    "gmail": frozenset({GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE}),
    "drive": frozenset({DRIVE_FILE_SCOPE, DRIVE_METADATA_SCOPE, DRIVE_SCOPE, DRIVE_READONLY_SCOPE}),
    "contacts": frozenset({CONTACTS_SCOPE, CONTACTS_READONLY_SCOPE}),
}
GOOGLE_CORE_PRODUCT_SCOPES: dict[str, frozenset[str]] = {
    "sheets": frozenset({SHEETS_SCOPE, SHEETS_READONLY_SCOPE}),
    "calendar": frozenset({
        CALENDAR_SCOPE,
        CALENDAR_READONLY_SCOPE,
        CALENDAR_EVENTS_READONLY_SCOPE,
    }),
    "gmail": frozenset({GMAIL_READONLY_SCOPE}),
    "drive": frozenset({DRIVE_METADATA_SCOPE}),
    "contacts": frozenset({CONTACTS_SCOPE, CONTACTS_READONLY_SCOPE}),
}
ALLOWED_GOOGLE_SCOPES = frozenset().union(*GOOGLE_PRODUCT_SCOPES.values())


class GoogleWorkspaceSettings(BaseSettings):
    """Settings used by the isolated Google Workspace MCP subprocess."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    google_redirect_uri: str = Field(default="")
    google_refresh_token: str = Field(default="")
    google_scopes: str = Field(default=SHEETS_SCOPE)
    google_token_storage_mode: str = Field(default="local")
    google_token_file: str = Field(default=".google_tokens.json")

    enable_gmail_send: bool = Field(default=False)
    enable_destructive_tools: bool = Field(default=False)
    log_level: str = Field(default="info")
    mcp_server_name: str = Field(default="chota-google-workspace-mcp")

    @property
    def scopes(self) -> list[str]:
        """Return configured OAuth scopes from comma or whitespace separated env."""

        parts = self.google_scopes.replace(",", " ").split()
        return [scope.strip() for scope in parts if scope.strip()]


@lru_cache
def get_google_settings() -> GoogleWorkspaceSettings:
    """Return cached Google Workspace MCP settings."""

    return GoogleWorkspaceSettings()
