"""Gmail API adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp_clients.servers.google_workspace.auth.oauth import (
    GoogleAuthError,
    build_credentials,
    validate_required_scopes,
)
from mcp_clients.servers.google_workspace.config.settings import GoogleWorkspaceSettings


class GmailClient:
    """Small synchronous Gmail API wrapper with async boundaries."""

    def __init__(self, settings: GoogleWorkspaceSettings) -> None:
        self.settings = settings
        self._service: Any | None = None

    def _get_service(self, required_scopes: list[str]) -> Any:
        validate_required_scopes(self.settings.scopes, required_scopes)
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
            self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return self._service

    async def search_messages(self, *, query: str, max_results: int, required_scopes: list[str]) -> list[dict[str, Any]]:
        service = self._get_service(required_scopes)
        response = await asyncio.to_thread(
            lambda: service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
            ).execute()
        )
        messages = response.get("messages", [])
        return messages if isinstance(messages, list) else []

    async def get_thread(self, *, thread_id: str, required_scopes: list[str]) -> dict[str, Any]:
        service = self._get_service(required_scopes)
        return await asyncio.to_thread(
            lambda: service.users().threads().get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject", "Date", "Message-ID", "References"],
            ).execute()
        )

    async def create_draft(self, *, raw_message: str, thread_id: str | None, required_scopes: list[str]) -> dict[str, Any]:
        service = self._get_service(required_scopes)
        message: dict[str, Any] = {"raw": raw_message}
        if thread_id:
            message["threadId"] = thread_id
        return await asyncio.to_thread(
            lambda: service.users().drafts().create(userId="me", body={"message": message}).execute()
        )

    async def send_draft(self, *, draft_id: str, required_scopes: list[str]) -> dict[str, Any]:
        service = self._get_service(required_scopes)
        return await asyncio.to_thread(
            lambda: service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        )
