from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from models.agent import AgentCredentials
from scripts.encrypt_mcp_credentials import backfill_credentials
from services.credentials_cipher import (
    CredentialEnvelopeError,
    decrypt_credentials,
    encrypt_credentials,
    is_encrypted_envelope,
)
from scripts import encrypt_mcp_credentials


@pytest.mark.integration
async def test_backfill_is_bounded_and_idempotent(api_client, db_session) -> None:
    owner_id = uuid4()
    agent_response = await api_client.post(
        "/v1/agents/",
        json={
            "name": "Backfill agent",
            "system_prompt": "Be concise.",
            "model_name": "google/gemini-2.5-flash",
            "temperature": 0.4,
        },
        headers={"x-user-id": str(owner_id)},
    )
    agent_id = UUID(agent_response.json()["id"])
    async with db_session() as session:
        session.add_all(
            [
                AgentCredentials(
                    agent_id=agent_id,
                    service_name="legacy",
                    credentials={"token": "test-only-backfill-secret"},
                ),
                AgentCredentials(
                    agent_id=agent_id,
                    service_name="already-encrypted",
                    credentials=encrypt_credentials({"token": "other-test-secret"}),
                ),
            ]
        )
        await session.commit()

    first = await backfill_credentials(db_session, batch_size=1, max_passes=2)
    second = await backfill_credentials(db_session, batch_size=1, max_passes=2)

    assert first.rows_encrypted == 1
    assert first.rows_already_encrypted == 1
    assert second.rows_encrypted == 0
    assert second.rows_already_encrypted == 2
    async with db_session() as session:
        result = await session.scalars(
            select(AgentCredentials).where(AgentCredentials.agent_id == agent_id)
        )
        records = list(result.all())
    assert all(is_encrypted_envelope(record.credentials) for record in records)
    assert {
        record.service_name: decrypt_credentials(record.credentials)["token"]
        for record in records
    } == {
        "legacy": "test-only-backfill-secret",
        "already-encrypted": "other-test-secret",
    }


@pytest.mark.integration
async def test_backfill_fails_closed_for_unknown_envelope(api_client, db_session) -> None:
    owner_id = uuid4()
    agent_response = await api_client.post(
        "/v1/agents/",
        json={
            "name": "Malformed envelope agent",
            "system_prompt": "Be concise.",
            "model_name": "google/gemini-2.5-flash",
            "temperature": 0.4,
        },
        headers={"x-user-id": str(owner_id)},
    )
    agent_id = UUID(agent_response.json()["id"])
    async with db_session() as session:
        session.add(
            AgentCredentials(
                agent_id=agent_id,
                service_name="broken",
                credentials=["chota:mcp-credentials", 2, "not-a-real-token"],
            )
        )
        await session.commit()

    with pytest.raises(CredentialEnvelopeError):
        await backfill_credentials(db_session, batch_size=1, max_passes=1)


def test_backfill_cli_failure_output_does_not_include_exception_text(
    monkeypatch, capsys
) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_credentials_encryption_enabled", True)

    async def raise_with_secret(*_args):
        raise RuntimeError("synthetic-secret-value")

    monkeypatch.setattr(encrypt_mcp_credentials, "_run_backfill", raise_with_secret)

    assert encrypt_mcp_credentials.main([]) == 1
    stderr = capsys.readouterr().err
    assert "RuntimeError" in stderr
    assert "synthetic-secret-value" not in stderr
