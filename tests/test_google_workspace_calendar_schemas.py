import pytest
from pydantic import ValidationError

from mcp_clients.servers.google_workspace.schemas.calendar import DeleteEventInput, UpdateEventInput


def test_update_event_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        UpdateEventInput.model_validate({
            "calendarId": "primary",
            "eventId": "event-1",
            "dryRun": True,
        })


def test_update_event_requires_confirmation_for_real_write() -> None:
    with pytest.raises(ValidationError):
        UpdateEventInput.model_validate({
            "calendarId": "primary",
            "eventId": "event-1",
            "title": "New title",
            "dryRun": False,
            "requiresConfirmation": False,
        })


def test_update_event_allows_unconfirmed_dry_run() -> None:
    payload = UpdateEventInput.model_validate({
        "calendarId": "primary",
        "eventId": "event-1",
        "title": "New title",
        "dryRun": True,
        "requiresConfirmation": False,
    })

    assert payload.dryRun is True


def test_delete_event_requires_confirmation_for_real_delete() -> None:
    with pytest.raises(ValidationError):
        DeleteEventInput.model_validate({
            "calendarId": "primary",
            "eventId": "event-1",
            "dryRun": False,
            "requiresConfirmation": False,
        })
