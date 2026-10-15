"""Install the parent loopback guard before pytest imports app/config code."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.goal5_business_bridge import network

private_path = Path(os.environ.get("GOAL5_PRIVATE_CONFIG", ""))
if not private_path.is_absolute() or not private_path.is_file():
    raise SystemExit("Set GOAL5_PRIVATE_CONFIG to a mode-0600 paired config")
if private_path.stat().st_mode & 0o777 != 0o600:
    raise SystemExit("Goal5 private config must have mode 0600")
config = json.loads(private_path.read_text())
if config.get("suite") != "goal5-business-bridge" or config.get("disposable") != "yes":
    raise SystemExit("Explicit Goal5 disposable paired config required")
ai_root = Path(config.get("ai_root", "")).resolve()
cs_root = Path(config.get("cs_root", "")).resolve()
pg_bin = Path(config.get("pg_bin", "")).resolve()
if ai_root != Path(__file__).resolve().parents[2]:
    raise SystemExit("Goal5 AI candidate does not match this test harness")
if not (cs_root / "README.md").is_file() or (cs_root / ".env").exists():
    raise SystemExit("Explicit valid Conversation candidate required; .env is forbidden")
if not (pg_bin / "psql").is_file():
    raise SystemExit("Explicit native PostgreSQL bin path is required")
endpoints = set()
names = set()
for kind in ("ai", "conversation", "provider"):
    url = urlsplit(config["databases"][kind]["url"])
    database = url.path.removeprefix("/")
    if (url.scheme != "postgresql+asyncpg" or url.hostname != "127.0.0.1"
            or url.port != config["port"] or not database
            or f"goal5_bridge_{kind}_test_" not in database
            or not url.username or not url.password):
        raise SystemExit("Only explicit numeric-loopback Goal5 test databases are allowed")
    endpoints.add((url.hostname, int(url.port)))
    names.add(database)
if len(names) != 3:
    raise SystemExit("Goal5 requires three distinct disposable service databases")
network.PREIMPORT_GUARD = network.install_guard(endpoints)
network.self_check()

# Keep only the execution path and the one explicit private fixture selector.
safe_environment = {key: os.environ[key] for key in ("PATH", "GOAL5_PRIVATE_CONFIG")
                    if key in os.environ}
latency_evidence = os.environ.get("GOAL5_LATENCY_EVIDENCE")
if latency_evidence:
    latency_path = Path(latency_evidence)
    if (not latency_path.is_absolute() or not latency_path.parent.is_dir()
            or latency_path.exists()):
        raise SystemExit("Latency evidence output must be a new absolute path in an existing directory")
    safe_environment["GOAL5_LATENCY_EVIDENCE"] = str(latency_path)
safe_environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                        GOAL5_CS_ROOT=str(cs_root))
os.environ.clear()
os.environ.update(safe_environment)

import pytest  # noqa: E402 -- import only after clearing inherited application environment

raise SystemExit(pytest.main([
    "-q", "-p", "no:cacheprovider",
    "tests/goal5_business_bridge/acceptance_http.py",
    "tests/goal5_business_bridge/latency_http.py", *sys.argv[1:],
]))
