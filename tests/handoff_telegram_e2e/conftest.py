"""Fixtures for the opt-in, loopback-only Handoff process acceptance suite."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.goal5_business_bridge.harness import close_harness, listen, prepare_harness
from tests.handoff_telegram_e2e.harness import (
    configure_handoff_services,
    close_handoff_services,
    start_handoff_services,
)

AI_ROOT = Path(__file__).resolve().parents[2]
if not os.environ.get("HANDOFF_E2E_PRIVATE_CONFIG"):
    collect_ignore = ["acceptance_http.py"]


def _private_config() -> dict:
    path = Path(os.environ["HANDOFF_E2E_PRIVATE_CONFIG"])
    if not path.is_absolute() or path.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError("Explicit mode-0600 Handoff E2E config required")
    value = json.loads(path.read_text())
    if value.get("suite") != "handoff-telegram-e2e" or value.get("disposable") != "yes":
        raise RuntimeError("Explicit disposable Handoff E2E config required")
    if Path(value["ai_root"]).resolve() != AI_ROOT.resolve():
        raise RuntimeError("Handoff E2E AI worktree does not match this test suite")
    for root_key in ("conversation_root", "control_plane_root", "telegram_root"):
        root = Path(value[root_key]).resolve()
        if not (root / "README.md").is_file() or (root / ".env").exists():
            raise RuntimeError("Candidate worktree is invalid or contains a .env file")
    # The reusable Goal5 process starter calls Conversation `cs` by its local
    # fixture name; the private Handoff config keeps service names explicit.
    value["cs_root"] = value["conversation_root"]
    value["cs_python"] = value["conversation_python"]
    return value


def _migration(kind: str, config: dict, workdir: Path) -> None:
    parsed = urlsplit(config["databases"][kind]["url"])
    if (parsed.scheme != "postgresql+asyncpg" or parsed.hostname != "127.0.0.1"
            or parsed.port != int(config["port"]) or not parsed.username or not parsed.password):
        raise RuntimeError("Migration target must be an explicit numeric-loopback test database")
    root_key = {
        "ai": "ai_root", "conversation": "conversation_root",
        "control_plane": "control_plane_root",
    }[kind]
    service_root = Path(config[root_key]).resolve()
    spec = workdir / f"{kind}-migration.json"
    spec.write_text(json.dumps({
        "service_root": str(service_root),
        "allowlist": [["127.0.0.1", parsed.port]],
    }))
    spec.chmod(0o600)
    python = str(config[f"{kind}_python"])
    env = {
        "PATH": f"{Path(python).parent}:{Path(config['pg_bin']).resolve()}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "HANDOFF_E2E_MIGRATION_CONFIG": str(spec),
    }
    if kind == "conversation":
        env["DATABASE_URL"] = config["databases"][kind]["url"]
    else:
        env.update(
            POSTGRES_HOST="127.0.0.1",
            POSTGRES_PORT=str(parsed.port),
            POSTGRES_DB=parsed.path.removeprefix("/"),
            POSTGRES_USER=parsed.username,
            POSTGRES_PASSWORD=parsed.password,
            POSTGRES_ECHO="false",
        )
    log = workdir / f"{kind}-migration.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            result = subprocess.run(
                [python, str(AI_ROOT / "tests/handoff_telegram_e2e/migrate.py")],
                cwd=AI_ROOT, env=env, stdout=handle, stderr=handle, timeout=120,
            )
    finally:
        spec.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"Owned {kind} migration failed; inspect its private log")


@pytest_asyncio.fixture(scope="session")
async def handoff(tmp_path_factory):
    config = _private_config()
    migration_dir = tmp_path_factory.mktemp("handoff-e2e-migrations")
    for kind in ("ai", "conversation", "control_plane"):
        _migration(kind, config, migration_dir)

    engines = {
        kind: create_async_engine(value["url"], poolclass=NullPool, hide_parameters=True)
        for kind, value in config["databases"].items()
    }
    names = (
        "provider", "ai", "cs", "ai_proxy", "effect_proxy", "control_plane",
        "conversation_worker", "tg_gateway", "user", "telegram_api",
    )
    listeners = {name: listen() for name in names}
    client = httpx.AsyncClient(trust_env=False, timeout=60)
    base = None
    try:
        base = await prepare_harness(config, config["databases"],
                                     tmp_path_factory.mktemp("handoff-e2e-run"),
                                     client, engines, listeners)
        await start_handoff_services(base, config)
        yield base
        base.safe_logs()
    finally:
        if base is not None:
            try:
                await close_handoff_services(base)
            finally:
                await close_harness(base)
        await client.aclose()
        for listener in listeners.values():
            try:
                listener.close()
            except OSError:
                pass
