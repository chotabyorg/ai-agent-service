"""Bounded, resumable backfill of legacy MCP credentials into envelopes.

Run as a PostSync job only after every serving pod supports encrypted reads and
``MCP_CREDENTIALS_ENCRYPTION_ENABLED`` is true. The command never prints row
identifiers or credential contents.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings
from models.agent import AgentCredentials
from services.credentials_cipher import (
    CredentialEnvelopeError,
    decrypt_credentials,
    encrypt_credentials,
    is_encrypted_envelope,
)

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 500
DEFAULT_MAX_PASSES = 3
MAX_PASSES = 10


class CredentialBackfillError(RuntimeError):
    """The backfill could not safely finish; rerun after resolving the cause."""


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    rows_scanned: int
    rows_encrypted: int
    rows_already_encrypted: int
    passes: int


SessionFactory = Callable[[], AsyncSession]


async def _count_legacy_objects(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        result = await session.scalar(
            select(func.count())
            .select_from(AgentCredentials)
            .where(func.jsonb_typeof(AgentCredentials.credentials) == "object")
        )
    return int(result or 0)


async def backfill_credentials(
    session_factory: SessionFactory,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_passes: int = DEFAULT_MAX_PASSES,
) -> BackfillSummary:
    """Encrypt legacy objects in bounded transactions and verify completion.

    A row lock plus a compare-and-set update prevents overwriting concurrent
    credential changes. Each committed batch is safe to repeat after a crash.
    A final plaintext-object count makes concurrent plaintext writers fail the
    job instead of producing a false success.
    """

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    if not 1 <= max_passes <= MAX_PASSES:
        raise ValueError(f"max_passes must be between 1 and {MAX_PASSES}.")

    rows_scanned = 0
    rows_encrypted = 0
    rows_already_encrypted = 0

    for pass_number in range(1, max_passes + 1):
        last_key: tuple[UUID, str] | None = None

        while True:
            async with session_factory() as session:
                async with session.begin():
                    statement = (
                        select(AgentCredentials)
                        .order_by(AgentCredentials.agent_id, AgentCredentials.service_name)
                        .limit(batch_size)
                        .with_for_update()
                    )
                    if last_key is not None:
                        statement = statement.where(
                            tuple_(AgentCredentials.agent_id, AgentCredentials.service_name)
                            > tuple_(last_key[0], last_key[1])
                        )
                    result = await session.scalars(statement)
                    batch = list(result.all())

                    if not batch:
                        break

                    for record in batch:
                        rows_scanned += 1
                        if is_encrypted_envelope(record.credentials):
                            # Validate ciphertext/version before treating the
                            # row as complete. Never silently skip bad envelopes.
                            decrypt_credentials(record.credentials)
                            rows_already_encrypted += 1
                            continue

                        if not isinstance(record.credentials, dict):
                            # Reject non-object legacy data and malformed arrays.
                            decrypt_credentials(record.credentials)
                            raise CredentialEnvelopeError(
                                "Stored MCP credentials have an unsupported format."
                            )

                        legacy_payload = record.credentials
                        encrypted = encrypt_credentials(legacy_payload)
                        result = await session.execute(
                            update(AgentCredentials)
                            .where(
                                AgentCredentials.agent_id == record.agent_id,
                                AgentCredentials.service_name == record.service_name,
                                AgentCredentials.credentials == legacy_payload,
                            )
                            .values(credentials=encrypted)
                            .execution_options(synchronize_session=False)
                        )
                        if result.rowcount != 1:
                            raise CredentialBackfillError(
                                "A credential row changed during backfill; rerun the command."
                            )
                        rows_encrypted += 1

                    last_record = batch[-1]
                    last_key = (last_record.agent_id, last_record.service_name)

        remaining = await _count_legacy_objects(session_factory)
        if remaining == 0:
            return BackfillSummary(
                rows_scanned=rows_scanned,
                rows_encrypted=rows_encrypted,
                rows_already_encrypted=rows_already_encrypted,
                passes=pass_number,
            )

    raise CredentialBackfillError(
        "Legacy MCP credentials remain after the bounded backfill passes."
    )


def _safe_error_type(exc: Exception) -> str:
    name = type(exc).__name__
    if name.isascii() and name.isidentifier() and len(name) <= 64:
        return name
    return "Exception"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-passes", type=int, default=DEFAULT_MAX_PASSES)
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= args.max_passes <= MAX_PASSES:
        parser.error(f"--max-passes must be between 1 and {MAX_PASSES}")
    return args


async def _run_backfill(batch_size: int, max_passes: int) -> BackfillSummary:
    settings = get_settings()
    migration_engine = create_async_engine(
        settings.postgres_dsn,
        echo=False,
        hide_parameters=True,
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        bind=migration_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        return await backfill_credentials(
            session_factory,
            batch_size=batch_size,
            max_passes=max_passes,
        )
    finally:
        await migration_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not get_settings().mcp_credentials_encryption_enabled:
        print(
            "MCP_CREDENTIALS_ENCRYPTION_ENABLED must be true before backfill.",
            file=sys.stderr,
        )
        return 2

    try:
        summary = asyncio.run(_run_backfill(args.batch_size, args.max_passes))
    except Exception as exc:
        # Avoid printing exception text/tracebacks because SQL and crypto errors
        # can carry credential values in their context or bound parameters.
        print(f"mcp_credentials_backfill_failed error_type={_safe_error_type(exc)}", file=sys.stderr)
        return 1

    print(
        "mcp_credentials_backfill_complete "
        f"rows_scanned={summary.rows_scanned} "
        f"rows_encrypted={summary.rows_encrypted} "
        f"rows_already_encrypted={summary.rows_already_encrypted} "
        f"passes={summary.passes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
