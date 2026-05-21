import pytest

from mcp_clients.servers.google_workspace.schemas.calendar import CalendarAttendee
from mcp_clients.servers.google_workspace.schemas.common import LeadInput
from mcp_clients.servers.google_workspace.services.calendar_service import (
    build_event_patch,
    build_event_payload,
    build_followup_payload,
    parse_event_datetime,
)


def test_parse_event_datetime_accepts_z_suffix() -> None:
    parsed = parse_event_datetime("2026-06-01T12:00:00Z")

    assert parsed.isoformat() == "2026-06-01T12:00:00+00:00"


def test_build_event_payload_rejects_end_before_start() -> None:
    with pytest.raises(ValueError):
        build_event_payload(
            title="Call",
            description=None,
            start="2026-06-01T13:00:00Z",
            end="2026-06-01T12:00:00Z",
            attendees=[],
            location=None,
        )


def test_build_event_payload_includes_attendees_and_lead_context() -> None:
    body = build_event_payload(
        title="Discovery call",
        description="Discuss proposal",
        start="2026-06-01T12:00:00Z",
        end="2026-06-01T12:30:00Z",
        attendees=[CalendarAttendee(email="lead@example.com", displayName="Lead")],
        location="Google Meet",
        lead_context=LeadInput(name="Lead", company="Example", email="LEAD@EXAMPLE.COM"),
    )

    assert body["summary"] == "Discovery call"
    assert body["attendees"] == [{"email": "lead@example.com", "displayName": "Lead", "optional": False}]
    assert body["location"] == "Google Meet"
    assert "Lead context:" in body["description"]
    assert "- email: lead@example.com" in body["description"]


def test_build_followup_payload_uses_lead_email_as_attendee() -> None:
    body = build_followup_payload(
        lead=LeadInput(name="Alice", company="Example", email="Alice@Example.com"),
        follow_up_at="2026-06-01T12:00:00Z",
        description="Follow up on quote",
        duration_minutes=45,
    )

    assert body["summary"] == "Follow up with Alice (Example)"
    assert body["attendees"][0]["email"] == "alice@example.com"
    assert body["end"]["dateTime"] == "2026-06-01T12:45:00+00:00"


def test_build_event_patch_rejects_invalid_time_window() -> None:
    with pytest.raises(ValueError):
        build_event_patch(
            title=None,
            description=None,
            start="2026-06-01T13:00:00Z",
            end="2026-06-01T12:00:00Z",
            attendees=None,
            location=None,
        )
