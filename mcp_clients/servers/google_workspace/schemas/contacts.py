"""Pydantic schemas for Google Contacts tools."""

from typing import Any

from pydantic import Field, field_validator, model_validator

from mcp_clients.servers.google_workspace.schemas.common import ToolInput


class ContactInput(ToolInput):
    name: str | None = Field(default=None, max_length=255)
    company: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    telegram: str | None = Field(default=None, max_length=128)
    website: str | None = Field(default=None, max_length=512)
    notes: str | None = Field(default=None, max_length=5000)
    customFields: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_contact_data(self) -> "ContactInput":
        if not any(
            value
            for value in (
                self.name,
                self.company,
                self.email,
                self.phone,
                self.telegram,
                self.website,
                self.notes,
                self.customFields,
            )
        ):
            raise ValueError("At least one contact field must be provided.")
        return self


class ContactsSearchInput(ToolInput):
    query: str = Field(min_length=1, max_length=512)
    maxResults: int = Field(default=10, ge=1, le=30)


class ContactsUpsertInput(ToolInput):
    contact: ContactInput
    dedupeKeys: list[str] = Field(default_factory=lambda: ["email", "phone", "telegram", "website"], min_length=1)
    dryRun: bool = False

    @field_validator("dedupeKeys")
    @classmethod
    def validate_dedupe_keys(cls, value: list[str]) -> list[str]:
        allowed = {"email", "phone", "telegram", "website", "name", "company"}
        for item in value:
            if not item or (item not in allowed and not item.startswith("custom:")):
                raise ValueError("dedupeKeys may contain contact fields or custom:<field> keys.")
        return value

    @model_validator(mode="after")
    def require_searchable_identity(self) -> "ContactsUpsertInput":
        if self.dryRun:
            return self
        searchable_values = (
            self.contact.email,
            self.contact.phone,
            self.contact.telegram,
            self.contact.website,
            self.contact.name,
            self.contact.company,
        )
        has_custom_value = any(
            key.startswith("custom:")
            and self.contact.customFields.get(key.removeprefix("custom:"))
            for key in self.dedupeKeys
        )
        if not any(searchable_values) and not has_custom_value:
            raise ValueError("Non-dry-run contact upsert requires a searchable identity field.")
        return self
