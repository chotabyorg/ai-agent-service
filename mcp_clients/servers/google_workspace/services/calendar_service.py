"""Calendar business logic for lead follow-ups."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp_clients.servers.google_workspace.schemas.calendar import CalendarAttendee
from mcp_clients.servers.google_workspace.schemas.common import LeadInput
from mcp_clients.servers.google_workspace.services.lead_service import normalize_lead


DEFAULT_TIMEZONE = "UTC"


def parse_event_datetime(value: str) -> datetime:
    """Parse an ISO datetime string and require a concrete date/time."""

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Datetime must be ISO 8601, for example 2026-06-01T12:00:00Z.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    return parsed


def build_event_time(value: str) -> dict[str, str]:
    """Build a Google Calendar event time object."""

    parsed = parse_event_datetime(value)
    event_time = {"dateTime": parsed.isoformat()}
    if isinstance(parsed.tzinfo, ZoneInfo):
        event_time["timeZone"] = parsed.tzinfo.key
    return event_time


def build_attendees(attendees: list[CalendarAttendee]) -> list[dict[str, Any]]:
    """Build Google Calendar attendee payloads."""

    return [
        {
            key: value
            for key, value in {
                "email": attendee.email,
                "displayName": attendee.displayName,
                "optional": attendee.optional,
            }.items()
            if value not in (None, "")
        }
        for attendee in attendees
    ]


def build_lead_context_description(description: str | None, lead: LeadInput | None) -> str:
    """Append compact lead context to an event description."""

    parts = [description.strip()] if description else []
    if lead is not None:
        normalized = normalize_lead(lead)
        lines = ["Lead context:"]
        for key in ("name", "company", "email", "phone", "telegram", "website", "status", "owner"):
            value = normalized.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        if normalized.get("notes"):
            lines.append(f"- notes: {normalized['notes']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_event_payload(
    *,
    title: str,
    description: str | None,
    start: str,
    end: str,
    attendees: list[CalendarAttendee],
    location: str | None,
    lead_context: LeadInput | None = None,
) -> dict[str, Any]:
    """Build a Google Calendar event payload from validated tool input."""

    start_dt = parse_event_datetime(start)
    end_dt = parse_event_datetime(end)
    if end_dt <= start_dt:
        raise ValueError("Event end must be after event start.")

    body: dict[str, Any] = {
        "summary": title,
        "description": build_lead_context_description(description, lead_context),
        "start": build_event_time(start),
        "end": build_event_time(end),
    }
    if attendees:
        body["attendees"] = build_attendees(attendees)
    if location:
        body["location"] = location
    return body


def build_event_patch(
    *,
    title: str | None,
    description: str | None,
    start: str | None,
    end: str | None,
    attendees: list[CalendarAttendee] | None,
    location: str | None,
) -> dict[str, Any]:
    """Build a patch payload for a Calendar event update."""

    body: dict[str, Any] = {}
    if start is not None and end is not None:
        start_dt = parse_event_datetime(start)
        end_dt = parse_event_datetime(end)
        if end_dt <= start_dt:
            raise ValueError("Event end must be after event start.")
    if title is not None:
        body["summary"] = title
    if description is not None:
        body["description"] = description
    if start is not None:
        body["start"] = build_event_time(start)
    if end is not None:
        body["end"] = build_event_time(end)
    if attendees is not None:
        body["attendees"] = build_attendees(attendees)
    if location is not None:
        body["location"] = location
    return body


def build_followup_payload(
    *,
    lead: LeadInput,
    follow_up_at: str,
    description: str | None,
    duration_minutes: int,
) -> dict[str, Any]:
    """Build a follow-up reminder event for a lead."""

    lead_data = normalize_lead(lead)
    start_dt = parse_event_datetime(follow_up_at)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    title_bits = ["Follow up"]
    if lead_data.get("name"):
        title_bits.append(f"with {lead_data['name']}")
    if lead_data.get("company"):
        title_bits.append(f"({lead_data['company']})")

    attendees = []
    if lead_data.get("email"):
        attendees.append(CalendarAttendee(email=lead_data["email"], displayName=lead_data.get("name") or None))

    return build_event_payload(
        title=" ".join(title_bits),
        description=description or "Lead follow-up reminder.",
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        attendees=attendees,
        location=None,
        lead_context=lead,
    )
