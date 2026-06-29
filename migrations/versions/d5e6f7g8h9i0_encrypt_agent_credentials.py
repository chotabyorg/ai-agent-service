"""encrypt existing agent_credentials rows in place

Revision ID: d5e6f7g8h9i0
Revises: c4d5e6f7g8h9
Create Date: 2026-06-29 12:00:00.000000

Rewrites legacy plaintext ``agent_credentials.credentials`` JSONB values into
the encrypted envelope format used by ``services.credentials_cipher``. The
column type stays JSONB; only the row contents change.

The migration is idempotent: rows that already use the encrypted envelope
shape (``{"__enc__": "v1", "data": "..."}``) are skipped. ``downgrade``
restores plaintext by decrypting any envelopes it finds.
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from services.credentials_cipher import (
    decrypt_credentials,
    encrypt_credentials,
    is_encrypted_envelope,
)

# revision identifiers, used by Alembic.
revision: str = "d5e6f7g8h9i0"
down_revision: Union[str, None] = "c4d5e6f7g8h9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _iter_rows(connection) -> list[tuple[str, str, dict]]:
    rows = connection.execute(
        sa.text(
            "SELECT agent_id::text, service_name, credentials "
            "FROM agent_credentials"
        )
    ).fetchall()
    parsed: list[tuple[str, str, dict]] = []
    for agent_id, service_name, credentials in rows:
        if isinstance(credentials, str):
            credentials = json.loads(credentials)
        if not isinstance(credentials, dict):
            # Defensive: skip rows that somehow ended up non-dict — they cannot
            # be encrypted as MCP credentials anyway.
            continue
        parsed.append((agent_id, service_name, credentials))
    return parsed


def upgrade() -> None:
    connection = op.get_bind()
    for agent_id, service_name, credentials in _iter_rows(connection):
        if is_encrypted_envelope(credentials):
            continue
        encrypted = encrypt_credentials(credentials)
        connection.execute(
            sa.text(
                "UPDATE agent_credentials "
                "SET credentials = CAST(:credentials AS jsonb) "
                "WHERE agent_id = CAST(:agent_id AS uuid) "
                "  AND service_name = :service_name"
            ),
            {
                "credentials": json.dumps(encrypted),
                "agent_id": agent_id,
                "service_name": service_name,
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    for agent_id, service_name, credentials in _iter_rows(connection):
        if not is_encrypted_envelope(credentials):
            continue
        plaintext = decrypt_credentials(credentials)
        connection.execute(
            sa.text(
                "UPDATE agent_credentials "
                "SET credentials = CAST(:credentials AS jsonb) "
                "WHERE agent_id = CAST(:agent_id AS uuid) "
                "  AND service_name = :service_name"
            ),
            {
                "credentials": json.dumps(plaintext),
                "agent_id": agent_id,
                "service_name": service_name,
            },
        )
