"""Google Calendar MCP tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError
from mcp_clients.servers.google_workspace.config.settings import get_google_settings
from mcp_clients.servers.google_workspace.google.calendar_client import GoogleCalendarClient
from mcp_clients.servers.google_workspace.schemas.calendar import (
    CreateEventInput,
    CreateFollowUpInput,
    DeleteEventInput,
    ListCalendarsInput,
    SearchEventsInput,
    UpdateEventInput,
)
from mcp_clients.servers.google_workspace.schemas.common import LeadInput
from mcp_clients.servers.google_workspace.services.calendar_service import (
    build_event_patch,
    build_event_payload,
    build_followup_payload,
)
from mcp_clients.servers.google_workspace.utils.responses import (
    error_response,
    google_api_error_response,
    success_response,
)


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        return error_response(
            "VALIDATION_ERROR",
            "Tool input validation failed.",
            details={"errors": json.loads(exc.json())},
        )
    if isinstance(exc, GoogleAuthError):
        return error_response(exc.code, str(exc), recoverable=exc.recoverable)
    if isinstance(exc, ValueError):
        return error_response("VALIDATION_ERROR", str(exc), recoverable=True)
    return google_api_error_response(exc, "Google Calendar")


def _get_calendar_client() -> GoogleCalendarClient:
    return GoogleCalendarClient(get_google_settings())


def register_calendar_tools(mcp: Any, tool: Callable[[str, str], Callable]) -> None:
    """Register Google Calendar tools on a FastMCP server."""

    @tool("google_calendar", "core")
    async def google_calendar_list_calendars() -> dict[str, Any]:
        """List Google calendars visible to the authorized account."""

        try:
            ListCalendarsInput.model_validate({})
            calendars = await _get_calendar_client().list_calendars()
            return success_response(
                action="calendars_listed",
                resource={"type": "google_calendar_list"},
                summary=f"Found {len(calendars)} calendars.",
                data={"calendars": calendars},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_calendar", "core")
    async def google_calendar_search_events(
        query: str | None = None,
        timeMin: str | None = None,
        timeMax: str | None = None,
        calendarId: str = "primary",
        maxResults: int = 20,
    ) -> dict[str, Any]:
        """Search Google Calendar events by query and optional time bounds."""

        try:
            payload = SearchEventsInput.model_validate(locals())
            events = await _get_calendar_client().search_events(
                calendar_id=payload.calendarId,
                query=payload.query,
                time_min=payload.timeMin,
                time_max=payload.timeMax,
                max_results=payload.maxResults,
            )
            return success_response(
                action="calendar_events_searched",
                resource={"type": "google_calendar", "calendarId": payload.calendarId},
                summary=f"Found {len(events)} matching events.",
                data={"events": events},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_calendar", "extended")
    async def google_calendar_create_event(
        title: str,
        start: str,
        end: str,
        calendarId: str = "primary",
        description: str | None = None,
        attendees: list[dict[str, Any]] | None = None,
        location: str | None = None,
        leadContext: dict[str, Any] | None = None,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Create a Google Calendar event. Honors dryRun for write safety."""

        try:
            payload = CreateEventInput.model_validate({
                "calendarId": calendarId,
                "title": title,
                "description": description,
                "start": start,
                "end": end,
                "attendees": attendees or [],
                "location": location,
                "leadContext": LeadInput.model_validate(leadContext) if leadContext else None,
                "dryRun": dryRun,
            })
            body = build_event_payload(
                title=payload.title,
                description=payload.description,
                start=payload.start,
                end=payload.end,
                attendees=payload.attendees,
                location=payload.location,
                lead_context=payload.leadContext,
            )
            if payload.dryRun:
                return success_response(
                    action="calendar_event_create_dry_run",
                    resource={"type": "google_calendar", "calendarId": payload.calendarId},
                    dry_run=True,
                    summary="Dry run: calendar event creation was validated but not executed.",
                    data={"event": body},
                )
            event = await _get_calendar_client().create_event(payload.calendarId, body)
            return success_response(
                action="calendar_event_created",
                resource={
                    "type": "google_calendar_event",
                    "calendarId": payload.calendarId,
                    "eventId": event.get("id"),
                },
                summary="Calendar event created.",
                data={"event": event},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_calendar", "extended")
    async def google_calendar_update_event(
        eventId: str,
        calendarId: str = "primary",
        title: str | None = None,
        description: str | None = None,
        start: str | None = None,
        end: str | None = None,
        attendees: list[dict[str, Any]] | None = None,
        location: str | None = None,
        dryRun: bool = False,
        requiresConfirmation: bool = True,
    ) -> dict[str, Any]:
        """Update a Google Calendar event. Non-dry-run updates require explicit confirmation."""

        try:
            payload = UpdateEventInput.model_validate(locals())
            body = build_event_patch(
                title=payload.title,
                description=payload.description,
                start=payload.start,
                end=payload.end,
                attendees=payload.attendees,
                location=payload.location,
            )
            if payload.dryRun:
                return success_response(
                    action="calendar_event_update_dry_run",
                    resource={
                        "type": "google_calendar_event",
                        "calendarId": payload.calendarId,
                        "eventId": payload.eventId,
                    },
                    dry_run=True,
                    summary="Dry run: calendar event update was validated but not executed.",
                    data={"eventPatch": body},
                )
            event = await _get_calendar_client().update_event(payload.calendarId, payload.eventId, body)
            return success_response(
                action="calendar_event_updated",
                resource={
                    "type": "google_calendar_event",
                    "calendarId": payload.calendarId,
                    "eventId": payload.eventId,
                },
                summary="Calendar event updated.",
                data={"event": event},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_calendar", "extended")
    async def google_calendar_delete_event(
        eventId: str,
        calendarId: str = "primary",
        dryRun: bool = False,
        requiresConfirmation: bool = True,
    ) -> dict[str, Any]:
        """Delete a Google Calendar event. Requires confirmation and ENABLE_DESTRUCTIVE_TOOLS=true."""

        try:
            payload = DeleteEventInput.model_validate(locals())
            if payload.dryRun:
                return success_response(
                    action="calendar_event_delete_dry_run",
                    resource={
                        "type": "google_calendar_event",
                        "calendarId": payload.calendarId,
                        "eventId": payload.eventId,
                    },
                    dry_run=True,
                    summary="Dry run: calendar event deletion was validated but not executed.",
                )
            settings = get_google_settings()
            if not settings.enable_destructive_tools:
                return error_response(
                    "DESTRUCTIVE_TOOLS_DISABLED",
                    "Calendar event deletion requires ENABLE_DESTRUCTIVE_TOOLS=true.",
                    recoverable=True,
                )
            await _get_calendar_client().delete_event(payload.calendarId, payload.eventId)
            return success_response(
                action="calendar_event_deleted",
                resource={
                    "type": "google_calendar_event",
                    "calendarId": payload.calendarId,
                    "eventId": payload.eventId,
                },
                summary="Calendar event deleted.",
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_calendar", "extended")
    async def google_calendar_create_followup(
        lead: dict[str, Any],
        followUpAt: str,
        calendarId: str = "primary",
        description: str | None = None,
        durationMinutes: int = 30,
        dryRun: bool = False,
        requiresConfirmation: bool = True,
    ) -> dict[str, Any]:
        """Create a Google Calendar follow-up reminder for a lead."""

        try:
            payload = CreateFollowUpInput.model_validate({
                "calendarId": calendarId,
                "lead": LeadInput.model_validate(lead),
                "followUpAt": followUpAt,
                "description": description,
                "durationMinutes": durationMinutes,
                "dryRun": dryRun,
                "requiresConfirmation": requiresConfirmation,
            })
            body = build_followup_payload(
                lead=payload.lead,
                follow_up_at=payload.followUpAt,
                description=payload.description,
                duration_minutes=payload.durationMinutes,
            )
            if payload.dryRun:
                return success_response(
                    action="calendar_followup_create_dry_run",
                    resource={"type": "google_calendar", "calendarId": payload.calendarId},
                    dry_run=True,
                    summary="Dry run: follow-up creation was validated but not executed.",
                    data={"event": body},
                )
            event = await _get_calendar_client().create_event(payload.calendarId, body)
            return success_response(
                action="calendar_followup_created",
                resource={
                    "type": "google_calendar_event",
                    "calendarId": payload.calendarId,
                    "eventId": event.get("id"),
                },
                summary="Follow-up event created.",
                data={"event": event},
            )
        except Exception as exc:
            return _tool_error(exc)
