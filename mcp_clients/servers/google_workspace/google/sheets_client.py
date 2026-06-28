"""Google Sheets API adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError, build_credentials, validate_required_scopes
from mcp_clients.servers.google_workspace.config.settings import SHEETS_READONLY_SCOPE, SHEETS_SCOPE, GoogleWorkspaceSettings


class GoogleSheetsClient:
    """Small synchronous Google Sheets API wrapper with async boundaries."""

    def __init__(self, settings: GoogleWorkspaceSettings) -> None:
        self.settings = settings
        self._service: Any | None = None

    def _get_service(self) -> Any:
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError

                raise GoogleAuthError(
                    "GOOGLE_DEPENDENCY_MISSING",
                    "google-api-python-client is not installed.",
                    recoverable=False,
                ) from exc

            try:
                validate_required_scopes(self.settings.scopes, [SHEETS_SCOPE])
                credentials = build_credentials(self.settings, [SHEETS_SCOPE])
            except GoogleAuthError:
                credentials = build_credentials(self.settings, [SHEETS_READONLY_SCOPE])
            self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._service

    async def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            lambda: self._get_service().spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        )

    async def create_spreadsheet(self, title: str) -> dict[str, Any]:
        body = {"properties": {"title": title}}
        return await asyncio.to_thread(lambda: self._get_service().spreadsheets().create(body=body).execute())

    async def create_sheet(self, spreadsheet_id: str, title: str) -> dict[str, Any]:
        body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
        return await asyncio.to_thread(
            lambda: self._get_service()
            .spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
            .execute()
        )

    async def read_range(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        response = await asyncio.to_thread(
            lambda: self._get_service()
            .spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        values = response.get("values", [])
        return values if isinstance(values, list) else []

    async def append_row(self, spreadsheet_id: str, range_name: str, values: list[Any]) -> dict[str, Any]:
        body = {"values": [values]}
        return await asyncio.to_thread(
            lambda: self._get_service()
            .spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body,
            )
            .execute()
        )

    async def update_range(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
    ) -> dict[str, Any]:
        body = {"values": values}
        return await asyncio.to_thread(
            lambda: self._get_service()
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="USER_ENTERED",
                body=body,
            )
            .execute()
        )
