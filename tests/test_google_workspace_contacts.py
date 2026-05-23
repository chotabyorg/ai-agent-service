import json

import pytest
from pydantic import ValidationError

from mcp_clients.manager import MCPManager
from mcp_clients.servers.google_workspace.schemas.contacts import ContactsUpsertInput
from mcp_clients.servers.google_workspace.services.contacts_service import (
    build_contact_search_query,
    build_person_payload,
    find_duplicate_contact,
)


GOOGLE_STAGE_4_CREDENTIALS = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "redirect_uri": "http://localhost/oauth2/callback",
    "refresh_token": "refresh-token",
    "scopes": (
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose "
        "https://www.googleapis.com/auth/drive.file "
        "https://www.googleapis.com/auth/drive.metadata.readonly "
        "https://www.googleapis.com/auth/contacts"
    ),
    "token_storage_mode": "local",
}


def _tool_payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_contacts_upsert_rejects_unknown_dedupe_key() -> None:
    with pytest.raises(ValidationError):
        ContactsUpsertInput.model_validate({
            "contact": {"email": "lead@example.com"},
            "dedupeKeys": ["unknown"],
        })


def test_contacts_upsert_rejects_empty_contact() -> None:
    with pytest.raises(ValidationError):
        ContactsUpsertInput.model_validate({"contact": {}})


def test_contacts_upsert_rejects_non_dry_run_without_searchable_identity() -> None:
    with pytest.raises(ValidationError):
        ContactsUpsertInput.model_validate({
            "contact": {"notes": "Internal note only"},
            "dryRun": False,
        })


def test_contacts_upsert_allows_dry_run_without_searchable_identity() -> None:
    payload = ContactsUpsertInput.model_validate({
        "contact": {"notes": "Internal note only"},
        "dryRun": True,
    })

    assert payload.dryRun is True


def test_build_person_payload_normalizes_contact_fields() -> None:
    person = build_person_payload(ContactsUpsertInput.model_validate({
        "contact": {
            "name": "Alice Example",
            "company": "Example Inc",
            "email": "ALICE@Example.com",
            "phone": "+1 (555) 100-2000",
            "telegram": "@alice",
            "website": "https://www.example.com/path",
            "notes": "Qualified lead",
        }
    }).contact)

    assert person["names"] == [{"givenName": "Alice", "familyName": "Example"}]
    assert person["emailAddresses"][0]["value"] == "alice@example.com"
    assert person["phoneNumbers"][0]["value"] == "+15551002000"
    assert person["organizations"][0]["name"] == "Example Inc"
    assert person["urls"][0]["value"] == "example.com"
    assert person["urls"][1]["value"] == "https://t.me/alice"


def test_find_duplicate_contact_by_email() -> None:
    result = {
        "person": {
            "resourceName": "people/1",
            "emailAddresses": [{"value": "lead@example.com"}],
        }
    }

    duplicate, matched_key = find_duplicate_contact(
        [result],
        ContactsUpsertInput.model_validate({"contact": {"email": "LEAD@example.com"}}).contact,
        ["email"],
    )

    assert duplicate["resourceName"] == "people/1"
    assert matched_key == "email"


def test_build_contact_search_query_uses_first_available_dedupe_value() -> None:
    payload = ContactsUpsertInput.model_validate({
        "contact": {"email": "", "phone": "+1 (555) 100-2000"},
        "dedupeKeys": ["email", "phone"],
    })

    assert build_contact_search_query(payload.contact, payload.dedupeKeys) == "+15551002000"


@pytest.mark.asyncio
async def test_google_workspace_mcp_subprocess_lists_contacts_tools() -> None:
    async with MCPManager("google", GOOGLE_STAGE_4_CREDENTIALS) as manager:
        names = {tool.name for tool in manager._tools}
        metadata = await manager.get_tools_metadata()

    assert "google_contacts_search" in names
    assert "google_contacts_upsert_contact" in names
    assert metadata["google_contacts_upsert_contact"] == {"category": "google_contacts", "tier": "core"}


@pytest.mark.asyncio
async def test_contacts_upsert_dry_run_does_not_require_google_call() -> None:
    async with MCPManager("google", GOOGLE_STAGE_4_CREDENTIALS) as manager:
        result = await manager.call_tool(
            "google_contacts_upsert_contact",
            {
                "contact": {"name": "Alice Example", "email": "alice@example.com"},
                "dryRun": True,
            },
        )

    payload = _tool_payload(result)
    assert payload["ok"] is True
    assert payload["action"] == "contact_upsert_dry_run"
    assert payload["data"]["person"]["emailAddresses"][0]["value"] == "alice@example.com"
