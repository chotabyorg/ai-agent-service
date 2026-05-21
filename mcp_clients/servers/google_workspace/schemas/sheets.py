"""Pydantic schemas for Google Sheets tools."""

from typing import Any

from pydantic import Field, field_validator, model_validator

from mcp_clients.servers.google_workspace.schemas.common import LeadIdentifier, LeadInput, ToolInput


class SpreadsheetInput(ToolInput):
    spreadsheetId: str = Field(min_length=1, max_length=256)


class ListTabsInput(SpreadsheetInput):
    pass


class ReadRangeInput(SpreadsheetInput):
    range: str = Field(min_length=1, max_length=512)


class AppendRowInput(SpreadsheetInput):
    sheetName: str = Field(min_length=1, max_length=100)
    values: list[Any] = Field(min_length=1, max_length=200)
    dryRun: bool = False


class UpdateRangeInput(SpreadsheetInput):
    range: str = Field(min_length=1, max_length=512)
    values: list[list[Any]] = Field(min_length=1, max_length=500)
    dryRun: bool = False
    requiresConfirmation: bool = True

    @model_validator(mode="after")
    def require_confirmation_for_write(self) -> "UpdateRangeInput":
        if not self.dryRun and not self.requiresConfirmation:
            raise ValueError("requiresConfirmation must be true for update_range writes.")
        return self


class SearchRowsInput(SpreadsheetInput):
    sheetName: str = Field(min_length=1, max_length=100)
    headerRow: int = Field(default=1, ge=1, le=100)
    filters: dict[str, Any] = Field(default_factory=dict)


class CreateSpreadsheetInput(ToolInput):
    title: str = Field(min_length=1, max_length=255)
    dryRun: bool = False


class CreateSheetInput(SpreadsheetInput):
    title: str = Field(min_length=1, max_length=100)
    dryRun: bool = False


class UpsertLeadInput(SpreadsheetInput):
    sheetName: str = Field(min_length=1, max_length=100)
    lead: LeadInput
    dedupeKeys: list[str] = Field(default_factory=lambda: ["email", "phone", "telegram", "website"])
    dryRun: bool = False
    headerRow: int = Field(default=1, ge=1, le=100)

    @field_validator("dedupeKeys")
    @classmethod
    def validate_dedupe_keys(cls, value: list[str]) -> list[str]:
        allowed = {"email", "phone", "telegram", "website", "name", "company"}
        for item in value:
            if not item or (item not in allowed and not item.startswith("custom:")):
                raise ValueError("dedupeKeys may contain lead fields or custom:<field> keys.")
        return value


class UpdateLeadStatusInput(SpreadsheetInput):
    sheetName: str = Field(min_length=1, max_length=100)
    leadIdentifier: LeadIdentifier
    status: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=5000)
    dryRun: bool = False
    headerRow: int = Field(default=1, ge=1, le=100)
