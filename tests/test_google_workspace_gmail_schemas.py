import pytest
from pydantic import ValidationError

from mcp_clients.servers.google_workspace.schemas.gmail import (
    GmailCreateDraftInput,
    GmailReplyToThreadInput,
    GmailSendDraftInput,
)


def test_create_draft_coerces_string_recipient() -> None:
    payload = GmailCreateDraftInput.model_validate({
        "to": "lead@example.com",
        "subject": "Hello",
        "body": "Body",
    })

    assert payload.to == ["lead@example.com"]


def test_send_draft_requires_confirmation() -> None:
    with pytest.raises(ValidationError):
        GmailSendDraftInput.model_validate({
            "draftId": "draft-1",
            "requiresConfirmation": False,
        })


def test_reply_to_thread_requires_confirmation_for_real_draft() -> None:
    with pytest.raises(ValidationError):
        GmailReplyToThreadInput.model_validate({
            "threadId": "thread-1",
            "body": "Reply",
            "requiresConfirmation": False,
            "dryRun": False,
        })


def test_reply_to_thread_allows_unconfirmed_dry_run() -> None:
    payload = GmailReplyToThreadInput.model_validate({
        "threadId": "thread-1",
        "body": "Reply",
        "requiresConfirmation": False,
        "dryRun": True,
    })

    assert payload.dryRun is True
