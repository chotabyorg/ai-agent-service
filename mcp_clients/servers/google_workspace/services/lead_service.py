"""Lead management logic backed by Google Sheets."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from mcp_clients.servers.google_workspace.schemas.common import LeadIdentifier, LeadInput
from mcp_clients.servers.google_workspace.utils.sheets import build_full_rows_range, build_sheet_range


DEFAULT_LEAD_HEADERS = [
    "name",
    "company",
    "email",
    "phone",
    "telegram",
    "website",
    "source",
    "status",
    "notes",
    "lastContactedAt",
    "nextFollowUpAt",
    "owner",
    "customFields",
]

DEFAULT_LEAD_STATUSES = {
    "new",
    "contacted",
    "interested",
    "meeting_scheduled",
    "proposal_sent",
    "won",
    "lost",
    "do_not_contact",
}


class SheetsClientProtocol(Protocol):
    async def read_range(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        ...

    async def append_row(self, spreadsheet_id: str, range_name: str, values: list[Any]) -> dict[str, Any]:
        ...

    async def update_range(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
    ) -> dict[str, Any]:
        ...


def normalize_email(value: str | None) -> str:
    """Normalize an email for matching."""

    return (value or "").strip().lower()


def normalize_phone(value: str | None) -> str:
    """Normalize a phone number to digits with an optional leading plus."""

    raw = (value or "").strip()
    if not raw:
        return ""
    leading_plus = raw.startswith("+")
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return ""
    return f"+{digits}" if leading_plus else digits


def normalize_telegram(value: str | None) -> str:
    """Normalize Telegram usernames for matching."""

    return (value or "").strip().lower().lstrip("@")


def normalize_website(value: str | None) -> str:
    """Normalize a website/domain for matching."""

    raw = (value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc or parsed.path
    host = host.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host.rstrip(".")


def normalize_lead(lead: LeadInput) -> dict[str, Any]:
    """Normalize a lead payload while preserving display values."""

    data = lead.model_dump()
    data["email"] = normalize_email(data.get("email"))
    data["phone"] = normalize_phone(data.get("phone"))
    data["telegram"] = normalize_telegram(data.get("telegram"))
    data["website"] = normalize_website(data.get("website"))
    data["status"] = data.get("status") or "new"
    return data


def _now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_header(value: Any) -> str:
    return str(value or "").strip()


def _row_to_record(headers: list[str], row: list[Any], row_number: int) -> dict[str, Any]:
    record = {header: row[index] if index < len(row) else "" for index, header in enumerate(headers)}
    record["_rowNumber"] = row_number
    return record


def _record_match_value(record: dict[str, Any], key: str) -> str:
    if key.startswith("custom:"):
        custom_key = key.removeprefix("custom:")
        custom_fields = record.get("customFields") or {}
        if isinstance(custom_fields, str):
            try:
                custom_fields = json.loads(custom_fields) if custom_fields else {}
            except json.JSONDecodeError:
                custom_fields = {}
        if isinstance(custom_fields, dict):
            return str(custom_fields.get(custom_key) or "").strip().lower()
        return ""
    if key == "email":
        return normalize_email(record.get("email"))
    if key == "phone":
        return normalize_phone(record.get("phone"))
    if key == "telegram":
        return normalize_telegram(record.get("telegram"))
    if key == "website":
        return normalize_website(record.get("website"))
    return str(record.get(key) or "").strip().lower()


def _lead_match_value(lead: dict[str, Any], key: str) -> str:
    if key.startswith("custom:"):
        return str((lead.get("customFields") or {}).get(key.removeprefix("custom:")) or "").strip().lower()
    return str(lead.get(key) or "").strip().lower()


def find_duplicate(
    records: list[dict[str, Any]],
    lead: dict[str, Any],
    dedupe_keys: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Find the first duplicate record by configured dedupe keys."""

    for key in dedupe_keys:
        lead_value = _lead_match_value(lead, key)
        if not lead_value:
            continue
        record_key = key.removeprefix("custom:")
        for record in records:
            lookup_key = key if key.startswith("custom:") else record_key
            if _record_match_value(record, lookup_key) == lead_value:
                return record, key
    return None, None


def _merge_notes(existing: str | None, new_note: str | None) -> str:
    if not new_note:
        return existing or ""
    stamped_note = f"[{_now_stamp()}] {new_note.strip()}"
    if existing:
        return f"{existing}\n{stamped_note}"
    return stamped_note


def _lead_to_row(headers: list[str], lead: dict[str, Any], existing: dict[str, Any] | None = None) -> list[Any]:
    row: list[Any] = []
    for header in headers:
        if header == "customFields":
            custom_fields = lead.get("customFields") or {}
            value = json.dumps(custom_fields, ensure_ascii=False, sort_keys=True) if custom_fields else ""
        elif header == "notes":
            value = _merge_notes((existing or {}).get("notes"), lead.get("notes"))
        else:
            incoming = lead.get(header)
            value = incoming if incoming not in (None, "") else (existing or {}).get(header, "")
        row.append(value)
    return row


class LeadSheetService:
    """Reusable lead upsert/search/update logic for Google Sheets tables."""

    def __init__(self, sheets_client: SheetsClientProtocol) -> None:
        self.sheets_client = sheets_client

    async def load_records(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        header_row: int = 1,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        rows = await self.sheets_client.read_range(
            spreadsheet_id,
            build_full_rows_range(sheet_name, header_row),
        )
        if not rows:
            return DEFAULT_LEAD_HEADERS, []

        headers = [
            _normalize_header(value) or f"column_{index + 1}"
            for index, value in enumerate(rows[0])
        ]
        records = [
            _row_to_record(headers, row, row_number=header_row + index)
            for index, row in enumerate(rows[1:], start=1)
        ]
        return headers or DEFAULT_LEAD_HEADERS, records

    async def search_rows(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        filters: dict[str, Any],
        header_row: int = 1,
    ) -> dict[str, Any]:
        headers, records = await self.load_records(spreadsheet_id, sheet_name, header_row)
        matched = []
        for record in records:
            if all(_record_match_value(record, key) == str(value).strip().lower() for key, value in filters.items()):
                matched.append(record)
        return {"headers": headers, "rows": matched, "total": len(matched)}

    async def upsert_lead(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        lead: LeadInput,
        dedupe_keys: list[str],
        *,
        dry_run: bool,
        header_row: int = 1,
    ) -> dict[str, Any]:
        headers, records = await self.load_records(spreadsheet_id, sheet_name, header_row)
        normalized_lead = normalize_lead(lead)
        duplicate, matched_key = find_duplicate(records, normalized_lead, dedupe_keys)

        if duplicate:
            row_number = int(duplicate["_rowNumber"])
            new_row = _lead_to_row(headers, normalized_lead, duplicate)
            target_range = build_sheet_range(sheet_name, f"A{row_number}", f"{_column_name(len(headers))}{row_number}")
            if not dry_run:
                await self.sheets_client.update_range(spreadsheet_id, target_range, [new_row])
            return {
                "action": "lead_updated",
                "rowNumber": row_number,
                "matchedKey": matched_key,
                "dryRun": dry_run,
                "range": target_range,
            }

        new_row = _lead_to_row(headers, normalized_lead)
        append_range = build_sheet_range(sheet_name, f"A{header_row}")
        if not dry_run:
            if not records:
                header_target = build_sheet_range(sheet_name, f"A{header_row}", f"{_column_name(len(headers))}{header_row}")
                await self.sheets_client.update_range(spreadsheet_id, header_target, [headers])
            await self.sheets_client.append_row(spreadsheet_id, append_range, new_row)
        return {
            "action": "lead_created",
            "rowNumber": None,
            "matchedKey": None,
            "dryRun": dry_run,
            "range": append_range,
        }

    async def update_lead_status(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        identifier: LeadIdentifier,
        status: str,
        note: str | None,
        *,
        dry_run: bool,
        header_row: int = 1,
    ) -> dict[str, Any]:
        headers, records = await self.load_records(spreadsheet_id, sheet_name, header_row)
        lead = LeadInput(
            email=identifier.email,
            phone=identifier.phone,
            telegram=identifier.telegram,
            website=identifier.website,
            customFields={
                identifier.customKey: identifier.customValue
            } if identifier.customKey and identifier.customValue else {},
            status=status,
            notes=note,
        )
        normalized_lead = normalize_lead(lead)
        dedupe_keys = ["email", "phone", "telegram", "website"]
        if identifier.customKey:
            dedupe_keys.append(f"custom:{identifier.customKey}")

        duplicate, matched_key = find_duplicate(records, normalized_lead, dedupe_keys)
        if not duplicate:
            return {"action": "lead_not_found", "rowNumber": None, "matchedKey": None, "dryRun": dry_run}

        row_number = int(duplicate["_rowNumber"])
        updated = dict(duplicate)
        updated["status"] = status
        updated["notes"] = _merge_notes(duplicate.get("notes"), note)
        row = [updated.get(header, "") for header in headers]
        target_range = build_sheet_range(sheet_name, f"A{row_number}", f"{_column_name(len(headers))}{row_number}")
        if not dry_run:
            await self.sheets_client.update_range(spreadsheet_id, target_range, [row])
        return {
            "action": "lead_status_updated",
            "rowNumber": row_number,
            "matchedKey": matched_key,
            "dryRun": dry_run,
            "range": target_range,
        }


def _column_name(index: int) -> str:
    """Convert a 1-based column index to A1 notation."""

    if index < 1:
        raise ValueError("index must be >= 1")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result
