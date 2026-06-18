"""Google Contacts MCP tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError
from mcp_clients.servers.google_workspace.config.settings import CONTACTS_SCOPE, get_google_settings
from mcp_clients.servers.google_workspace.google.contacts_client import GoogleContactsClient
from mcp_clients.servers.google_workspace.schemas.contacts import (
    ContactInput,
    ContactsSearchInput,
    ContactsUpsertInput,
)
from mcp_clients.servers.google_workspace.services.contacts_service import (
    build_contact_search_query,
    build_person_payload,
    contact_resource,
    find_duplicate_contact,
)
from mcp_clients.servers.google_workspace.utils.responses import (
    error_response,
    google_api_error_response,
    success_response,
)


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        return error_response(
            "VALIDATION_ERROR",
            "Tool input validation failed.",
            details={"errors": json.loads(exc.json())},
        )
    if isinstance(exc, GoogleAuthError):
        return error_response(exc.code, str(exc), recoverable=exc.recoverable)
    if isinstance(exc, ValueError):
        return error_response("VALIDATION_ERROR", str(exc), recoverable=True)
    return google_api_error_response(exc, "Google Contacts")


def _get_contacts_client() -> GoogleContactsClient:
    return GoogleContactsClient(get_google_settings())


def register_contacts_tools(mcp: Any, tool: Callable[[str, str], Callable]) -> None:
    """Register Google Contacts tools on a FastMCP server."""

    @tool("google_contacts", "core")
    async def google_contacts_search(query: str, maxResults: int = 10) -> dict[str, Any]:
        """Search Google Contacts through the People API."""

        try:
            payload = ContactsSearchInput.model_validate({"query": query, "maxResults": maxResults})
            results = await _get_contacts_client().search_contacts(
                query=payload.query,
                max_results=payload.maxResults,
                required_scopes=[CONTACTS_SCOPE],
            )
            people = [result.get("person", result) for result in results]
            return success_response(
                action="contacts_searched",
                resource={"type": "google_contacts_search"},
                summary=f"Found {len(people)} matching contacts.",
                data={"contacts": people},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("google_contacts", "extended")
    async def google_contacts_upsert_contact(
        contact: dict[str, Any],
        dedupeKeys: list[str] | None = None,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Create or update a Google Contact with deduplication."""

        try:
            payload = ContactsUpsertInput.model_validate({
                "contact": ContactInput.model_validate(contact),
                "dedupeKeys": dedupeKeys or ["email", "phone", "telegram", "website"],
                "dryRun": dryRun,
            })
            person_payload = build_person_payload(payload.contact)
            query = build_contact_search_query(payload.contact, payload.dedupeKeys)
            if payload.dryRun:
                return success_response(
                    action="contact_upsert_dry_run",
                    resource={"type": "google_contact"},
                    dry_run=True,
                    summary="Dry run: contact upsert was validated but not executed.",
                    data={"searchQuery": query, "person": person_payload},
                )

            client = _get_contacts_client()
            results = await client.search_contacts(
                query=query,
                max_results=10,
                required_scopes=[CONTACTS_SCOPE],
            )
            duplicate, matched_key = find_duplicate_contact(results, payload.contact, payload.dedupeKeys)
            if duplicate:
                updated = await client.update_contact(
                    resource_name=duplicate["resourceName"],
                    person=person_payload,
                    etag=duplicate.get("etag"),
                    required_scopes=[CONTACTS_SCOPE],
                )
                return success_response(
                    action="contact_updated",
                    resource=contact_resource(updated),
                    summary=f"Contact updated because duplicate {matched_key} was found.",
                    data={"contact": updated, "matchedKey": matched_key},
                )

            created = await client.create_contact(person=person_payload, required_scopes=[CONTACTS_SCOPE])
            return success_response(
                action="contact_created",
                resource=contact_resource(created),
                summary="Contact created because no duplicate was found.",
                data={"contact": created},
            )
        except Exception as exc:
            return _tool_error(exc)
