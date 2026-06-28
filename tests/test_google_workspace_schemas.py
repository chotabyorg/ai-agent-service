import pytest
from pydantic import ValidationError

from mcp_clients.servers.google_workspace.schemas.sheets import UpdateRangeInput, UpsertLeadInput


def test_update_range_requires_confirmation_for_real_write() -> None:
    with pytest.raises(ValidationError):
        UpdateRangeInput.model_validate({
            "spreadsheetId": "sheet-1",
            "range": "Leads!A1:B2",
            "values": [["a", "b"]],
            "dryRun": False,
            "requiresConfirmation": False,
        })


def test_update_range_allows_unconfirmed_dry_run() -> None:
    payload = UpdateRangeInput.model_validate({
        "spreadsheetId": "sheet-1",
        "range": "Leads!A1:B2",
        "values": [["a", "b"]],
        "dryRun": True,
        "requiresConfirmation": False,
    })

    assert payload.dryRun is True


def test_upsert_lead_rejects_unknown_dedupe_key() -> None:
    with pytest.raises(ValidationError):
        UpsertLeadInput.model_validate({
            "spreadsheetId": "sheet-1",
            "sheetName": "Leads",
            "lead": {"email": "a@example.com"},
            "dedupeKeys": ["unknown"],
        })
