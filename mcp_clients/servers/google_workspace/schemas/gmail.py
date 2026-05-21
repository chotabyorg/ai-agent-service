"""Pydantic schemas for Gmail tools."""

from pydantic import Field, field_validator, model_validator

from mcp_clients.servers.google_workspace.schemas.common import LeadInput, ToolInput


class GmailSearchInput(ToolInput):
    query: str = Field(min_length=1, max_length=1024)
    maxResults: int = Field(default=10, ge=1, le=50)


class GmailReadThreadInput(ToolInput):
    threadId: str = Field(min_length=1, max_length=256)


class GmailCreateDraftInput(ToolInput):
    to: list[str] = Field(min_length=1, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=20000)
    leadContext: LeadInput | None = None
    dryRun: bool = False

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def coerce_recipients(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class GmailSendDraftInput(ToolInput):
    draftId: str = Field(min_length=1, max_length=256)
    requiresConfirmation: bool = True

    @model_validator(mode="after")
    def require_confirmation(self) -> "GmailSendDraftInput":
        if not self.requiresConfirmation:
            raise ValueError("requiresConfirmation must be true for sending Gmail drafts.")
        return self


class GmailReplyToThreadInput(ToolInput):
    threadId: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=20000)
    to: list[str] = Field(default_factory=list, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str | None = Field(default=None, max_length=512)
    leadContext: LeadInput | None = None
    dryRun: bool = False
    requiresConfirmation: bool = True

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def coerce_recipients(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @model_validator(mode="after")
    def require_confirmation_for_write(self) -> "GmailReplyToThreadInput":
        if not self.dryRun and not self.requiresConfirmation:
            raise ValueError("requiresConfirmation must be true for Gmail reply drafts.")
        return self
