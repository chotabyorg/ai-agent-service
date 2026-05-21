"""Google Calendar API adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError, build_credentials
from mcp_clients.servers.google_workspace.config.settings import CALENDAR_SCOPE, GoogleWorkspaceSettings


class GoogleCalendarClient:
    """Small synchronous Google Calendar API wrapper with async boundaries."""

    def __init__(self, settings: GoogleWorkspaceSettings) -> None:
        self.settings = settings
        self._service: Any | None = None

    def _get_service(self) -> Any:
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise GoogleAuthError(
                    "GOOGLE_DEPENDENCY_MISSING",
                    "google-api-python-client is not installed.",
                    recoverable=False,
                ) from exc

            credentials = build_credentials(self.settings, [CALENDAR_SCOPE])
            self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    async def list_calendars(self) -> list[dict[str, Any]]:
        response = await asyncio.to_thread(lambda: self._get_service().calendarList().list().execute())
        items = response.get("items", [])
        return items if isinstance(items, list) else []

    async def search_events(
        self,
        *,
        calendar_id: str,
        query: str | None,
        time_min: str | None,
        time_max: str | None,
        max_results: int,
    ) -> list[dict[str, Any]]:
        request = self._get_service().events().list(
            calendarId=calendar_id,
            q=query,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        response = await asyncio.to_thread(lambda: request.execute())
        items = response.get("items", [])
        return items if isinstance(items, list) else []

    async def create_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            lambda: self._get_service().events().insert(calendarId=calendar_id, body=body).execute()
        )

    async def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            lambda: self._get_service().events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
            ).execute()
        )

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await asyncio.to_thread(
            lambda: self._get_service().events().delete(calendarId=calendar_id, eventId=event_id).execute()
        )
