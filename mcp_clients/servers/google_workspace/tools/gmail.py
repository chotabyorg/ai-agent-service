"""Gmail MCP tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from mcp_clients.servers.google_workspace.auth.oauth import GoogleAuthError
from mcp_clients.servers.google_workspace.config.settings import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    get_google_settings,
)
from mcp_clients.servers.google_workspace.google.gmail_client import GmailClient
from mcp_clients.servers.google_workspace.schemas.common import LeadInput
from mcp_clients.servers.google_workspace.schemas.gmail import (
    GmailCreateDraftInput,
    GmailReadThreadInput,
    GmailReplyToThreadInput,
    GmailSearchInput,
    GmailSendDraftInput,
)
from mcp_clients.servers.google_workspace.services.gmail_service import (
    build_email_message,
    build_reply_headers,
    build_reply_recipients,
    build_reply_subject,
    summarize_thread,
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
    return google_api_error_response(exc, "Gmail")


def _get_gmail_client() -> GmailClient:
    return GmailClient(get_google_settings())


def register_gmail_tools(mcp: Any, tool: Callable[[str, str], Callable]) -> None:
    """Register Gmail tools on a FastMCP server."""

    @tool("gmail", "core")
    async def gmail_search_emails(query: str, maxResults: int = 10) -> dict[str, Any]:
        """Search Gmail messages. Requires gmail.readonly scope."""

        try:
            payload = GmailSearchInput.model_validate({"query": query, "maxResults": maxResults})
            messages = await _get_gmail_client().search_messages(
                query=payload.query,
                max_results=payload.maxResults,
                required_scopes=[GMAIL_READONLY_SCOPE],
            )
            return success_response(
                action="gmail_messages_searched",
                resource={"type": "gmail_messages"},
                summary=f"Found {len(messages)} matching messages.",
                data={"messages": messages},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("gmail", "core")
    async def gmail_read_thread(threadId: str) -> dict[str, Any]:
        """Read Gmail thread metadata and snippets. Requires gmail.readonly scope."""

        try:
            payload = GmailReadThreadInput.model_validate({"threadId": threadId})
            thread = await _get_gmail_client().get_thread(
                thread_id=payload.threadId,
                required_scopes=[GMAIL_READONLY_SCOPE],
            )
            summary = summarize_thread(thread)
            return success_response(
                action="gmail_thread_read",
                resource={"type": "gmail_thread", "threadId": payload.threadId},
                summary=f"Read Gmail thread with {summary['messageCount']} messages.",
                data={"thread": summary},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("gmail", "core")
    async def gmail_create_draft(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        leadContext: dict[str, Any] | None = None,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """Create a Gmail draft. Does not send email."""

        try:
            payload = GmailCreateDraftInput.model_validate({
                "to": to,
                "cc": cc or [],
                "bcc": bcc or [],
                "subject": subject,
                "body": body,
                "leadContext": LeadInput.model_validate(leadContext) if leadContext else None,
                "dryRun": dryRun,
            })
            message = build_email_message(
                to=payload.to,
                cc=payload.cc,
                bcc=payload.bcc,
                subject=payload.subject,
                body=payload.body,
                lead_context=payload.leadContext,
            )
            if payload.dryRun:
                return success_response(
                    action="gmail_draft_create_dry_run",
                    resource={"type": "gmail_draft"},
                    dry_run=True,
                    summary="Dry run: Gmail draft creation was validated but not executed.",
                    data={"message": {"rawLength": len(message["raw"])}},
                )
            draft = await _get_gmail_client().create_draft(
                raw_message=message["raw"],
                thread_id=None,
                required_scopes=[GMAIL_COMPOSE_SCOPE],
            )
            return success_response(
                action="gmail_draft_created",
                resource={"type": "gmail_draft", "draftId": draft.get("id")},
                summary="Gmail draft created.",
                data={"draft": draft},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("gmail", "core")
    async def gmail_send_draft(draftId: str, requiresConfirmation: bool = True) -> dict[str, Any]:
        """Send a Gmail draft only when explicitly confirmed and ENABLE_GMAIL_SEND=true."""

        try:
            payload = GmailSendDraftInput.model_validate({
                "draftId": draftId,
                "requiresConfirmation": requiresConfirmation,
            })
            settings = get_google_settings()
            if not settings.enable_gmail_send:
                return error_response(
                    "GMAIL_SEND_DISABLED",
                    "Gmail sending requires ENABLE_GMAIL_SEND=true.",
                    recoverable=True,
                )
            sent = await _get_gmail_client().send_draft(
                draft_id=payload.draftId,
                required_scopes=[GMAIL_SEND_SCOPE],
            )
            return success_response(
                action="gmail_draft_sent",
                resource={"type": "gmail_message", "draftId": payload.draftId, "messageId": sent.get("id")},
                summary="Gmail draft sent.",
                data={"message": sent},
            )
        except Exception as exc:
            return _tool_error(exc)

    @tool("gmail", "core")
    async def gmail_reply_to_thread(
        threadId: str,
        body: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str | None = None,
        leadContext: dict[str, Any] | None = None,
        dryRun: bool = False,
        requiresConfirmation: bool = True,
    ) -> dict[str, Any]:
        """Create a reply draft in an existing Gmail thread. Does not send email."""

        try:
            payload = GmailReplyToThreadInput.model_validate({
                "threadId": threadId,
                "body": body,
                "to": to or [],
                "cc": cc or [],
                "bcc": bcc or [],
                "subject": subject,
                "leadContext": LeadInput.model_validate(leadContext) if leadContext else None,
                "dryRun": dryRun,
                "requiresConfirmation": requiresConfirmation,
            })
            thread = await _get_gmail_client().get_thread(
                thread_id=payload.threadId,
                required_scopes=[GMAIL_READONLY_SCOPE],
            )
            recipients = build_reply_recipients(thread, payload.to)
            if not recipients:
                return error_response(
                    "GMAIL_REPLY_RECIPIENT_MISSING",
                    "Reply recipient could not be inferred from the thread.",
                    recoverable=True,
                )
            message = build_email_message(
                to=recipients,
                cc=payload.cc,
                bcc=payload.bcc,
                subject=build_reply_subject(thread, payload.subject),
                body=payload.body,
                lead_context=payload.leadContext,
                thread_id=payload.threadId,
                extra_headers=build_reply_headers(thread),
            )
            if payload.dryRun:
                return success_response(
                    action="gmail_reply_draft_create_dry_run",
                    resource={"type": "gmail_thread", "threadId": payload.threadId},
                    dry_run=True,
                    summary="Dry run: Gmail reply draft was validated but not created.",
                    data={"message": {"rawLength": len(message["raw"]), "to": recipients}},
                )
            draft = await _get_gmail_client().create_draft(
                raw_message=message["raw"],
                thread_id=payload.threadId,
                required_scopes=[GMAIL_COMPOSE_SCOPE],
            )
            return success_response(
                action="gmail_reply_draft_created",
                resource={"type": "gmail_draft", "threadId": payload.threadId, "draftId": draft.get("id")},
                summary="Gmail reply draft created.",
                data={"draft": draft},
            )
        except Exception as exc:
            return _tool_error(exc)
