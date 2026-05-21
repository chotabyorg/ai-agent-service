"""Google Sheets MCP tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError
from mcp_clients.servers.google_workspace.config.settings import get_google_settings
from mcp_clients.servers.google_workspace.google.sheets_client import GoogleSheetsClient
from mcp_clients.servers.google_workspace.schemas.common import LeadIdentifier, LeadInput
from mcp_clients.servers.google_workspace.schemas.sheets import (
    AppendRowInput,
    CreateSheetInput,
    CreateSpreadsheetInput,
    ListTabsInput,
    ReadRangeInput,
    SearchRowsInput,
    SpreadsheetInput,
    UpdateLeadStatusInput,
    UpdateRangeInput,
    UpsertLeadInput,
)
from mcp_clients.servers.google_workspace.services.lead_service import LeadSheetService
from mcp_clients.servers.google_workspace.utils.responses import error_response, success_response
from mcp_clients.servers.google_workspace.utils.sheets import build_sheet_range, parse_a1_range


TOOLS_META: dict[str, dict[str, str]] = {}


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        return error_response(
            "VALIDATION_ERROR",
            "Tool input validation failed.",
            details={"errors": json.loads(exc.json())},
        )
    if isinstance(exc, GoogleAuthError):
        return error_response(exc.code, str(exc), recoverable=exc.recoverable)
    return error_response(
        "GOOGLE_API_ERROR",
        "Google Workspace request failed.",
        details={"type": exc.__class__.__name__},
    )


def _get_sheets_service() -> tuple[GoogleSheetsClient, LeadSheetService]:
    client = GoogleSheetsClient(get_google_settings())
    return client, LeadSheetService(client)


def register_sheets_tools(mcp: Any, tool: Callable[[str, str], Callable]) -> None:
    """Register Google Sheets tools on a FastMCP server."""

    @tool("google_sheets", "core")
    async def google_sheets_get_spreadsheet(spreadsheetId: str) -> dict[str, Any]:
        """Read Google spreadsheet metadata by spreadsheetId."""

        try:
            payload = SpreadsheetInput.model_validate({"spreadsheetId": spreadsheetId})
            client, _ = _get_sheets_service()
            spreadsheet = await client.get_spreadsheet(payload.spreadsheetId)
            return success_response(
                action="spreadsheet_loaded",
                resource={"type": "google_spreadsheet", "spreadsheetId": payload.spreadsheetId},
                summary="Spreadsheet metadata loaded.",
                data=spreadsheet,
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_list_tabs(spreadsheetId: str) -> dict[str, Any]:
        """List sheets/tabs in a Google spreadsheet."""

        try:
            payload = ListTabsInput.model_validate({"spreadsheetId": spreadsheetId})
            client, _ = _get_sheets_service()
            spreadsheet = await client.get_spreadsheet(payload.spreadsheetId)
            tabs = [
                sheet.get("properties", {})
                for sheet in spreadsheet.get("sheets", [])
            ]
            return success_response(
                action="tabs_listed",
                resource={"type": "google_spreadsheet", "spreadsheetId": payload.spreadsheetId},
                summary=f"Found {len(tabs)} sheet tabs.",
                data={"tabs": tabs},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_read_range(spreadsheetId: str, range: str) -> dict[str, Any]:
        """Read values from a Google Sheets A1 range."""

        try:
            payload = ReadRangeInput.model_validate({"spreadsheetId": spreadsheetId, "range": range})
            parse_a1_range(payload.range)
            client, _ = _get_sheets_service()
            rows = await client.read_range(payload.spreadsheetId, payload.range)
            return success_response(
                action="range_read",
                resource={
                    "type": "google_sheet_range",
                    "spreadsheetId": payload.spreadsheetId,
                    "range": payload.range,
                },
                summary=f"Read {len(rows)} rows.",
                data={"values": rows},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_append_row(
        spreadsheetId: str,
        sheetName: str,
        values: list[Any],
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Append one row to a Google Sheet. Honors dryRun for write safety."""

        try:
            payload = AppendRowInput.model_validate(locals())
            append_range = build_sheet_range(payload.sheetName, "A1")
            if payload.dryRun:
                return success_response(
                    action="row_append_dry_run",
                    resource={
                        "type": "google_sheet_row",
                        "spreadsheetId": payload.spreadsheetId,
                        "sheetName": payload.sheetName,
                    },
                    dry_run=True,
                    summary="Dry run: row append was validated but not executed.",
                    data={"values": payload.values},
                )
            client, _ = _get_sheets_service()
            result = await client.append_row(payload.spreadsheetId, append_range, payload.values)
            return success_response(
                action="row_appended",
                resource={
                    "type": "google_sheet_row",
                    "spreadsheetId": payload.spreadsheetId,
                    "sheetName": payload.sheetName,
                },
                summary="Row appended.",
                data=result,
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_update_range(
        spreadsheetId: str,
        range: str,
        values: list[list[Any]],
        dryRun: bool = False,
        requiresConfirmation: bool = True,
    ) -> dict[str, Any]:
        """Update a Google Sheets range. Non-dry-run writes require explicit confirmation."""

        try:
            payload = UpdateRangeInput.model_validate(locals())
            parse_a1_range(payload.range)
            if payload.dryRun:
                return success_response(
                    action="range_update_dry_run",
                    resource={
                        "type": "google_sheet_range",
                        "spreadsheetId": payload.spreadsheetId,
                        "range": payload.range,
                    },
                    dry_run=True,
                    summary="Dry run: range update was validated but not executed.",
                    data={"values": payload.values},
                )
            client, _ = _get_sheets_service()
            result = await client.update_range(payload.spreadsheetId, payload.range, payload.values)
            return success_response(
                action="range_updated",
                resource={
                    "type": "google_sheet_range",
                    "spreadsheetId": payload.spreadsheetId,
                    "range": payload.range,
                },
                summary="Range updated.",
                data=result,
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_search_rows(
        spreadsheetId: str,
        sheetName: str,
        headerRow: int = 1,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Search rows by exact field/value filters using the sheet header row."""

        try:
            payload = SearchRowsInput.model_validate({
                "spreadsheetId": spreadsheetId,
                "sheetName": sheetName,
                "headerRow": headerRow,
                "filters": filters or {},
            })
            _, lead_service = _get_sheets_service()
            result = await lead_service.search_rows(
                payload.spreadsheetId,
                payload.sheetName,
                payload.filters,
                payload.headerRow,
            )
            return success_response(
                action="rows_searched",
                resource={
                    "type": "google_sheet",
                    "spreadsheetId": payload.spreadsheetId,
                    "sheetName": payload.sheetName,
                },
                summary=f"Found {result['total']} matching rows.",
                data=result,
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_create_spreadsheet(title: str, dryRun: bool = False) -> dict[str, Any]:
        """Create a new Google spreadsheet."""

        try:
            payload = CreateSpreadsheetInput.model_validate(locals())
            if payload.dryRun:
                return success_response(
                    action="spreadsheet_create_dry_run",
                    summary="Dry run: spreadsheet creation was validated but not executed.",
                    dry_run=True,
                    data={"title": payload.title},
                )
            client, _ = _get_sheets_service()
            result = await client.create_spreadsheet(payload.title)
            return success_response(
                action="spreadsheet_created",
                resource={"type": "google_spreadsheet", "spreadsheetId": result.get("spreadsheetId")},
                summary="Spreadsheet created.",
                data=result,
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_create_tab(
        spreadsheetId: str,
        title: str,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Create a new sheet/tab in a spreadsheet."""

        try:
            payload = CreateSheetInput.model_validate(locals())
            if payload.dryRun:
                return success_response(
                    action="sheet_tab_create_dry_run",
                    resource={"type": "google_spreadsheet", "spreadsheetId": payload.spreadsheetId},
                    summary="Dry run: sheet tab creation was validated but not executed.",
                    dry_run=True,
                    data={"title": payload.title},
                )
            client, _ = _get_sheets_service()
            result = await client.create_sheet(payload.spreadsheetId, payload.title)
            return success_response(
                action="sheet_tab_created",
                resource={"type": "google_spreadsheet", "spreadsheetId": payload.spreadsheetId},
                summary="Sheet tab created.",
                data=result,
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_upsert_lead(
        spreadsheetId: str,
        sheetName: str,
        lead: dict[str, Any],
        dedupeKeys: list[str] | None = None,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Create or update a lead row with deduplication by email, phone, Telegram, website or custom key."""

        try:
            payload = UpsertLeadInput.model_validate({
                "spreadsheetId": spreadsheetId,
                "sheetName": sheetName,
                "lead": LeadInput.model_validate(lead),
                "dedupeKeys": dedupeKeys or ["email", "phone", "telegram", "website"],
                "dryRun": dryRun,
            })
            _, lead_service = _get_sheets_service()
            result = await lead_service.upsert_lead(
                payload.spreadsheetId,
                payload.sheetName,
                payload.lead,
                payload.dedupeKeys,
                dry_run=payload.dryRun,
                header_row=payload.headerRow,
            )
            return success_response(
                action=result["action"],
                resource={
                    "type": "google_sheet_row",
                    "spreadsheetId": payload.spreadsheetId,
                    "sheetName": payload.sheetName,
                    "rowNumber": result.get("rowNumber"),
                },
                dry_run=payload.dryRun,
                summary=_lead_upsert_summary(result["action"], result.get("matchedKey")),
                data=result,
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_sheets", "core")
    async def google_sheets_update_lead_status(
        spreadsheetId: str,
        sheetName: str,
        leadIdentifier: dict[str, Any],
        status: str,
        note: str | None = None,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Update a lead status and append an optional timestamped note."""

        try:
            payload = UpdateLeadStatusInput.model_validate({
                "spreadsheetId": spreadsheetId,
                "sheetName": sheetName,
                "leadIdentifier": LeadIdentifier.model_validate(leadIdentifier),
                "status": status,
                "note": note,
                "dryRun": dryRun,
            })
            _, lead_service = _get_sheets_service()
            result = await lead_service.update_lead_status(
                payload.spreadsheetId,
                payload.sheetName,
                payload.leadIdentifier,
                payload.status,
                payload.note,
                dry_run=payload.dryRun,
                header_row=payload.headerRow,
            )
            ok = result["action"] != "lead_not_found"
            if not ok:
                return error_response("LEAD_NOT_FOUND", "No matching lead was found.", details=result)
            return success_response(
                action=result["action"],
                resource={
                    "type": "google_sheet_row",
                    "spreadsheetId": payload.spreadsheetId,
                    "sheetName": payload.sheetName,
                    "rowNumber": result.get("rowNumber"),
                },
                dry_run=payload.dryRun,
                summary="Lead status updated.",
                data=result,
            )
        except Exception as exc:
            return _tool_error(exc)


def _lead_upsert_summary(action: str, matched_key: str | None) -> str:
    if action == "lead_updated":
        return f"Lead was updated because duplicate {matched_key} was found."
    return "Lead was created because no duplicate was found."
