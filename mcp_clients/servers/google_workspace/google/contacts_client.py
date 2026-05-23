"""Google Contacts People API adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError, build_credentials
from mcp_clients.servers.google_workspace.config.settings import GoogleWorkspaceSettings


CONTACT_READ_MASK = "names,emailAddresses,phoneNumbers,organizations,urls,biographies"
CONTACT_UPDATE_FIELDS = "names,emailAddresses,phoneNumbers,organizations,urls,biographies"


class GoogleContactsClient:
    """Small synchronous Google People API wrapper with async boundaries."""

    def __init__(self, settings: GoogleWorkspaceSettings) -> None:
        self.settings = settings
        self._service: Any | None = None

    def _get_service(self, required_scopes: list[str]) -> Any:
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise GoogleAuthError(
                    "GOOGLE_DEPENDENCY_MISSING",
                    "google-api-python-client is not installed.",
                    recoverable=False,
                ) from exc
            credentials = build_credentials(self.settings, required_scopes)
            self._service = build("people", "v1", credentials=credentials, cache_discovery=False)
        return self._service

    async def search_contacts(
        self,
        *,
        query: str,
        max_results: int,
        required_scopes: list[str],
    ) -> list[dict[str, Any]]:
        service = self._get_service(required_scopes)
        response = await asyncio.to_thread(
            lambda: service.people().searchContacts(
                query=query,
                readMask=CONTACT_READ_MASK,
                pageSize=max_results,
            ).execute()
        )
        results = response.get("results", [])
        return results if isinstance(results, list) else []

    async def create_contact(self, *, person: dict[str, Any], required_scopes: list[str]) -> dict[str, Any]:
        service = self._get_service(required_scopes)
        return await asyncio.to_thread(lambda: service.people().createContact(body=person).execute())

    async def update_contact(
        self,
        *,
        resource_name: str,
        person: dict[str, Any],
        etag: str | None,
        required_scopes: list[str],
    ) -> dict[str, Any]:
        service = self._get_service(required_scopes)
        body = dict(person)
        body["resourceName"] = resource_name
        if etag:
            body["etag"] = etag
        return await asyncio.to_thread(
            lambda: service.people().updateContact(
                resourceName=resource_name,
                updatePersonFields=CONTACT_UPDATE_FIELDS,
                body=body,
            ).execute()
        )
