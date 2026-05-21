import base64
from email import message_from_bytes

from mcp_clients.servers.google_workspace.schemas.common import LeadInput
from mcp_clients.servers.google_workspace.services.gmail_service import (
    build_email_message,
    build_reply_headers,
    build_reply_recipients,
    build_reply_subject,
    summarize_thread,
)


THREAD = {
    "id": "thread-1",
    "historyId": "100",
    "messages": [
        {
            "id": "msg-1",
            "threadId": "thread-1",
            "snippet": "Hello",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Alice <alice@example.com>"},
                    {"name": "Subject", "value": "Pricing"},
                    {"name": "Message-ID", "value": "<msg-1@example.com>"},
                ]
            },
        }
    ],
}


def test_build_email_message_appends_lead_context() -> None:
    payload = build_email_message(
        to=["lead@example.com"],
        cc=[],
        bcc=[],
        subject="Proposal",
        body="Hello",
        lead_context=LeadInput(name="Lead", email="Lead@Example.com"),
    )
    decoded = message_from_bytes(base64.urlsafe_b64decode(payload["raw"]))

    assert decoded["To"] == "lead@example.com"
    assert decoded["Subject"] == "Proposal"
    body = decoded.get_payload()
    assert "Lead context:" in body
    assert "- email: lead@example.com" in body


def test_build_reply_subject_and_recipients_from_thread() -> None:
    assert build_reply_subject(THREAD) == "Re: Pricing"
    assert build_reply_recipients(THREAD, []) == ["Alice <alice@example.com>"]
    assert build_reply_headers(THREAD) == {
        "In-Reply-To": "<msg-1@example.com>",
        "References": "<msg-1@example.com>",
    }


def test_build_reply_recipients_prefers_explicit_to() -> None:
    assert build_reply_recipients(THREAD, ["owner@example.com"]) == ["owner@example.com"]


def test_summarize_thread_removes_bodies() -> None:
    summary = summarize_thread(THREAD)

    assert summary["id"] == "thread-1"
    assert summary["messageCount"] == 1
    assert summary["messages"][0]["headers"]["From"] == "Alice <alice@example.com>"
    assert "payload" not in summary["messages"][0]
