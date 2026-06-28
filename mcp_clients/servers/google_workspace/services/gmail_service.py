"""Gmail message and draft helpers."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

from mcp_clients.servers.google_workspace.schemas.common import LeadInput
from mcp_clients.servers.google_workspace.services.lead_service import normalize_lead


def _join_recipients(values: list[str]) -> str:
    return ", ".join(value.strip() for value in values if value and value.strip())


def append_lead_context(body: str, lead: LeadInput | None) -> str:
    """Append compact lead context to an email body."""

    if lead is None:
        return body
    normalized = normalize_lead(lead)
    lines = ["", "", "Lead context:"]
    for key in ("name", "company", "email", "phone", "telegram", "website", "status", "owner"):
        value = normalized.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    if normalized.get("notes"):
        lines.append(f"- notes: {normalized['notes']}")
    return body.rstrip() + "\n".join(lines)


def build_email_message(
    *,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body: str,
    lead_context: LeadInput | None = None,
    thread_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a Gmail raw message payload."""

    message = EmailMessage()
    message["To"] = _join_recipients(to)
    if cc:
        message["Cc"] = _join_recipients(cc)
    if bcc:
        message["Bcc"] = _join_recipients(bcc)
    message["Subject"] = subject
    for header_name, header_value in (extra_headers or {}).items():
        if header_value:
            message[header_name] = header_value
    message.set_content(append_lead_context(body, lead_context))
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return payload


def build_reply_subject(thread: dict[str, Any], fallback: str | None = None) -> str:
    """Build a reply subject from thread metadata."""

    subject = fallback or _first_header(thread, "Subject") or "Re:"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def build_reply_recipients(thread: dict[str, Any], explicit_to: list[str]) -> list[str]:
    """Use explicit recipients or reply to the last message From header."""

    if explicit_to:
        return explicit_to
    from_header = _last_header(thread, "From")
    return [from_header] if from_header else []


def build_reply_headers(thread: dict[str, Any]) -> dict[str, str]:
    """Build RFC 2822 reply headers from the latest thread message."""

    message_id = _last_header(thread, "Message-ID")
    references = _last_header(thread, "References")
    headers: dict[str, str] = {}
    if message_id:
        headers["In-Reply-To"] = message_id
        headers["References"] = f"{references} {message_id}".strip() if references else message_id
    return headers


def summarize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    """Return thread metadata without message bodies."""

    messages = thread.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    return {
        "id": thread.get("id"),
        "historyId": thread.get("historyId"),
        "messageCount": len(messages),
        "messages": [
            {
                "id": message.get("id"),
                "threadId": message.get("threadId"),
                "snippet": message.get("snippet"),
                "headers": _headers_map(message),
            }
            for message in messages
        ],
    }


def _headers_map(message: dict[str, Any]) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers", [])
    if not isinstance(headers, list):
        return {}
    return {
        header.get("name", ""): header.get("value", "")
        for header in headers
        if header.get("name")
    }


def _first_header(thread: dict[str, Any], name: str) -> str | None:
    messages = thread.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return None
    return _headers_map(messages[0]).get(name)


def _last_header(thread: dict[str, Any], name: str) -> str | None:
    messages = thread.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return None
    return _headers_map(messages[-1]).get(name)
