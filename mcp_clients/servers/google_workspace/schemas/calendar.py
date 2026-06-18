"""Pydantic schemas for Google Calendar tools."""

from pydantic import Field, field_validator, model_validator

from mcp_clients.servers.google_workspace.schemas.common import LeadInput, ToolInput


class CalendarAttendee(ToolInput):
    email: str = Field(min_length=3, max_length=320)
    displayName: str | None = Field(default=None, max_length=255)
    optional: bool = False


class ListCalendarsInput(ToolInput):
    pass


class SearchEventsInput(ToolInput):
    query: str | None = Field(default=None, max_length=512)
    timeMin: str | None = Field(default=None, max_length=64)
    timeMax: str | None = Field(default=None, max_length=64)
    calendarId: str = Field(default="primary", min_length=1, max_length=256)
    maxResults: int = Field(default=20, ge=1, le=50)


class CreateEventInput(ToolInput):
    calendarId: str = Field(default="primary", min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=5000)
    start: str = Field(min_length=1, max_length=64)
    end: str = Field(min_length=1, max_length=64)
    attendees: list[CalendarAttendee] = Field(default_factory=list, max_length=50)
    location: str | None = Field(default=None, max_length=1024)
    leadContext: LeadInput | None = None
    dryRun: bool = False

    @field_validator("attendees", mode="before")
    @classmethod
    def coerce_attendees(cls, value):
        if value is None:
            return []
        return value


class UpdateEventInput(ToolInput):
    calendarId: str = Field(default="primary", min_length=1, max_length=256)
    eventId: str = Field(min_length=1, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=5000)
    start: str | None = Field(default=None, min_length=1, max_length=64)
    end: str | None = Field(default=None, min_length=1, max_length=64)
    attendees: list[CalendarAttendee] | None = Field(default=None, max_length=50)
    location: str | None = Field(default=None, max_length=1024)
    dryRun: bool = False
    requiresConfirmation: bool = True

    @model_validator(mode="after")
    def require_update_and_confirmation(self) -> "UpdateEventInput":
        has_update = any(
            value is not None
            for value in (self.title, self.description, self.start, self.end, self.attendees, self.location)
        )
        if not has_update:
            raise ValueError("At least one event field must be provided.")
        if not self.dryRun and not self.requiresConfirmation:
            raise ValueError("requiresConfirmation must be true for event updates.")
        return self


class DeleteEventInput(ToolInput):
    calendarId: str = Field(default="primary", min_length=1, max_length=256)
    eventId: str = Field(min_length=1, max_length=512)
    dryRun: bool = False
    requiresConfirmation: bool = True

    @model_validator(mode="after")
    def require_confirmation(self) -> "DeleteEventInput":
        if not self.dryRun and not self.requiresConfirmation:
            raise ValueError("requiresConfirmation must be true for event deletion.")
        return self


class CreateFollowUpInput(ToolInput):
    calendarId: str = Field(default="primary", min_length=1, max_length=256)
    lead: LeadInput
    followUpAt: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=5000)
    durationMinutes: int = Field(default=30, ge=5, le=480)
    dryRun: bool = False
    requiresConfirmation: bool = True

    @model_validator(mode="after")
    def require_update_and_confirmation(self) -> "CreateFollowUpInput":
        if not self.dryRun and not self.requiresConfirmation:
            raise ValueError("requiresConfirmation must be true for follow-up creation.")
        return self
