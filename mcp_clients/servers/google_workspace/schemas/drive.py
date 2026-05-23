"""Pydantic schemas for Google Drive tools."""

from pydantic import Field, model_validator

from mcp_clients.servers.google_workspace.schemas.common import ToolInput


class DriveSearchFilesInput(ToolInput):
    query: str | None = Field(default=None, max_length=512)
    mimeType: str | None = Field(default=None, max_length=255)
    maxResults: int = Field(default=10, ge=1, le=50)


class DriveCreateFolderInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    parentFolderId: str | None = Field(default=None, max_length=256)
    dryRun: bool = False


class DriveCreateTextFileInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=1_000_000)
    mimeType: str = Field(default="text/plain", min_length=1, max_length=255)
    parentFolderId: str | None = Field(default=None, max_length=256)
    dryRun: bool = False

    @model_validator(mode="after")
    def restrict_text_mime_type(self) -> "DriveCreateTextFileInput":
        allowed = {"text/plain", "application/json", "text/csv", "text/markdown"}
        if self.mimeType not in allowed:
            raise ValueError("mimeType must be one of: text/plain, application/json, text/csv, text/markdown.")
        return self
