"""Guarded, explicit disposable databases for paired real-process tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.goal5_business_bridge.harness import (
    AI_ROOT,
    CS_ROOT,
    close_harness,
    listen,
    prepare_harness,
)


# Keep this process suite out of ordinary `pytest tests` runs; the dedicated
# `run.py` explicitly validates config and selects the file after the guard.
if not os.environ.get("GOAL5_PRIVATE_CONFIG"):
    collect_ignore = ["acceptance_http.py", "latency_http.py"]


def _private_config() -> tuple[Path, dict]:
    path = Path(os.environ["GOAL5_PRIVATE_CONFIG"])
    if not path.is_absolute() or path.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError("Explicit mode-0600 Goal5 configuration required")
    config = json.loads(path.read_text())
    if config.get("suite") != "goal5-business-bridge" or config.get("disposable") != "yes":
        raise RuntimeError("Explicit disposable Goal5 configuration required")
    if Path(config["ai_root"]).resolve() != AI_ROOT.resolve():
        raise RuntimeError("Goal5 AI candidate does not match this worktree")
    if CS_ROOT is None:
        raise RuntimeError("GOAL5_CS_ROOT is required for paired process tests")
    if Path(config["cs_root"]).resolve() != CS_ROOT.resolve():
        raise RuntimeError("Goal5 Conversation candidate does not match paired worktree")
    if (AI_ROOT / ".env").exists() or (CS_ROOT / ".env").exists():
        raise RuntimeError("Candidate .env files are forbidden")
    return path, config


def _migration(service: str, url: str, service_root: Path, python: str,
               pg_bin: str, workdir: Path):
    parsed = urlsplit(url)
    if (parsed.scheme != "postgresql+asyncpg" or parsed.hostname != "127.0.0.1"
            or parsed.port is None or parsed.username is None or parsed.password is None):
        raise RuntimeError("Only explicit loopback PostgreSQL migration URLs are allowed")
    spec = workdir / f"{service}-migration.json"
    spec.write_text(json.dumps({
        "allowlist": [["127.0.0.1", parsed.port]],
        "service_root": str(service_root),
    }))
    spec.chmod(0o600)
    env = {
        "PATH": f"{Path(python).parent}:{Path(pg_bin).resolve()}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "GOAL5_MIGRATION_CONFIG": str(spec),
    }
    if service == "ai":
        env.update(
            POSTGRES_HOST="127.0.0.1",
            POSTGRES_PORT=str(parsed.port),
            POSTGRES_DB=parsed.path.removeprefix("/"),
            POSTGRES_USER=parsed.username,
            POSTGRES_PASSWORD=parsed.password,
            POSTGRES_ECHO="false",
        )
    else:
        env["DATABASE_URL"] = url
    log_path = workdir / f"{service}-migration.log"
    log_path.chmod(0o600) if log_path.exists() else None
    with log_path.open("wb") as log:
        result = subprocess.run(
            [python, str(AI_ROOT / "tests/goal5_business_bridge/migrate.py")],
            cwd=service_root, env=env, stdout=log, stderr=log, timeout=90,
        )
    log_path.chmod(0o600)
    spec.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"Owned {service} migration failed; inspect private migration log")


@pytest.fixture(scope="session")
def paired_inputs(tmp_path_factory):
    private_path, config = _private_config()
    workdir = tmp_path_factory.mktemp("goal5-bridge-migrations")
    for kind, root_key, python_key in (
        ("ai", "ai_root", "ai_python"),
        ("conversation", "cs_root", "cs_python"),
    ):
        _migration(kind, config["databases"][kind]["url"], Path(config[root_key]),
                   config[python_key], config["pg_bin"], workdir)
    return private_path, config


@pytest_asyncio.fixture
async def bridge(paired_inputs, tmp_path):
    _, config = paired_inputs
    engines = {
        kind: create_async_engine(value["url"], poolclass=NullPool, hide_parameters=True)
        for kind, value in config["databases"].items()
    }
    listeners = {}
    client = httpx.AsyncClient(trust_env=False, timeout=55)
    harness = None
    try:
        for name in ("provider", "ai", "cs", "ai_proxy", "effect_proxy"):
            listeners[name] = listen()
        harness = await prepare_harness(config, config["databases"], tmp_path,
                                        client, engines, listeners)
        yield harness
        harness.safe_logs()
    finally:
        if harness is not None:
            await close_harness(harness)
        await client.aclose()
        for listener in listeners.values():
            try:
                listener.close()
            except OSError:
                pass
