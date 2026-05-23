"""Google Drive API adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError, build_credentials
from mcp_clients.servers.google_workspace.config.settings import GoogleWorkspaceSettings


class GoogleDriveClient:
    """Small synchronous Google Drive API wrapper with async boundaries."""

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
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    async def search_files(self, *, query: str, max_results: int, required_scopes: list[str]) -> list[dict[str, Any]]:
        service = self._get_service(required_scopes)
        response = await asyncio.to_thread(
            lambda: service.files().list(
                q=query,
                pageSize=max_results,
                fields="files(id,name,mimeType,webViewLink,modifiedTime,size,parents)",
            ).execute()
        )
        files = response.get("files", [])
        return files if isinstance(files, list) else []

    async def create_folder(
        self,
        *,
        name: str,
        parent_folder_id: str | None,
        required_scopes: list[str],
    ) -> dict[str, Any]:
        service = self._get_service(required_scopes)
        body: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        return await asyncio.to_thread(
            lambda: service.files().create(
                body=body,
                fields="id,name,mimeType,webViewLink,parents",
            ).execute()
        )

    async def create_text_file(
        self,
        *,
        name: str,
        content: str,
        mime_type: str,
        parent_folder_id: str | None,
        required_scopes: list[str],
    ) -> dict[str, Any]:
        try:
            from googleapiclient.http import MediaInMemoryUpload
        except ImportError as exc:
            raise GoogleAuthError(
                "GOOGLE_DEPENDENCY_MISSING",
                "google-api-python-client media upload support is not installed.",
                recoverable=False,
            ) from exc

        service = self._get_service(required_scopes)
        body: dict[str, Any] = {"name": name}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type, resumable=False)
        return await asyncio.to_thread(
            lambda: service.files().create(
                body=body,
                media_body=media,
                fields="id,name,mimeType,webViewLink,parents,size",
            ).execute()
        )
