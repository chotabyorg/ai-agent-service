from typing import Any

import pytest

from mcp_clients.servers.google_workspace.schemas.common import LeadIdentifier, LeadInput
from mcp_clients.servers.google_workspace.services.lead_service import (
    DEFAULT_LEAD_HEADERS,
    LeadSheetService,
    find_duplicate,
    normalize_email,
    normalize_phone,
    normalize_telegram,
    normalize_website,
)


class FakeSheetsClient:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows
        self.append_calls: list[tuple[str, str, list[Any]]] = []
        self.update_calls: list[tuple[str, str, list[list[Any]]]] = []

    async def read_range(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        return self.rows

    async def append_row(self, spreadsheet_id: str, range_name: str, values: list[Any]) -> dict[str, Any]:
        self.append_calls.append((spreadsheet_id, range_name, values))
        return {"updates": {"updatedRows": 1}}

    async def update_range(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
    ) -> dict[str, Any]:
        self.update_calls.append((spreadsheet_id, range_name, values))
        return {"updatedRows": len(values)}


def test_lead_normalizers() -> None:
    assert normalize_email(" USER@Example.COM ") == "user@example.com"
    assert normalize_phone("+1 (555) 100-2000") == "+15551002000"
    assert normalize_telegram("@ChotaBY") == "chotaby"
    assert normalize_website("https://www.Example.com/path?q=1") == "example.com"


def test_find_duplicate_by_email_and_custom_key() -> None:
    records = [
        {
            "_rowNumber": 2,
            "email": "lead@example.com",
            "customFields": '{"external_id": "abc-1"}',
        }
    ]

    duplicate, matched_key = find_duplicate(
        records,
        {"email": "", "customFields": {"external_id": "abc-1"}},
        ["email", "custom:external_id"],
    )

    assert duplicate == records[0]
    assert matched_key == "custom:external_id"


@pytest.mark.asyncio
async def test_upsert_lead_dry_run_does_not_write_new_lead() -> None:
    client = FakeSheetsClient([DEFAULT_LEAD_HEADERS])
    service = LeadSheetService(client)

    result = await service.upsert_lead(
        "spreadsheet-1",
        "Leads",
        LeadInput(name="Alice", email="Alice@Example.com"),
        ["email"],
        dry_run=True,
    )

    assert result["action"] == "lead_created"
    assert result["dryRun"] is True
    assert client.append_calls == []
    assert client.update_calls == []


@pytest.mark.asyncio
async def test_upsert_lead_updates_duplicate_email() -> None:
    client = FakeSheetsClient([
        DEFAULT_LEAD_HEADERS,
        ["Alice", "", "alice@example.com", "", "", "", "", "new", "old note"],
    ])
    service = LeadSheetService(client)

    result = await service.upsert_lead(
        "spreadsheet-1",
        "Leads",
        LeadInput(name="Alice Smith", email="ALICE@example.com", status="contacted", notes="called"),
        ["email"],
        dry_run=False,
    )

    assert result["action"] == "lead_updated"
    assert result["rowNumber"] == 2
    assert result["matchedKey"] == "email"
    assert len(client.update_calls) == 1
    assert client.update_calls[0][1] == "'Leads'!A2:M2"
    updated_row = client.update_calls[0][2][0]
    assert updated_row[0] == "Alice Smith"
    assert updated_row[7] == "contacted"
    assert "old note" in updated_row[8]
    assert "called" in updated_row[8]


@pytest.mark.asyncio
async def test_update_lead_status_returns_not_found_without_write() -> None:
    client = FakeSheetsClient([DEFAULT_LEAD_HEADERS])
    service = LeadSheetService(client)

    result = await service.update_lead_status(
        "spreadsheet-1",
        "Leads",
        LeadIdentifier(email="missing@example.com"),
        "lost",
        "No response",
        dry_run=False,
    )

    assert result["action"] == "lead_not_found"
    assert client.update_calls == []
