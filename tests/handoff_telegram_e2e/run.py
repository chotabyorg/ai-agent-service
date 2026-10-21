"""Install the loopback guard before collecting the non-default process suite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

AI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AI_ROOT))

from tests.goal5_business_bridge import network  # noqa: E402

private_path = Path(os.environ.get("HANDOFF_E2E_PRIVATE_CONFIG", ""))
if not private_path.is_absolute() or not private_path.is_file():
    raise SystemExit("Set HANDOFF_E2E_PRIVATE_CONFIG to the private disposable config")
if private_path.stat().st_mode & 0o777 != 0o600:
    raise SystemExit("Handoff E2E private config must have mode 0600")
config = json.loads(private_path.read_text())
if config.get("suite") != "handoff-telegram-e2e" or config.get("disposable") != "yes":
    raise SystemExit("Explicit disposable Handoff E2E config required")
if Path(config.get("ai_root", "")).resolve() != AI_ROOT.resolve():
    raise SystemExit("AI candidate does not match the test harness")
for key in ("conversation_root", "control_plane_root", "telegram_root"):
    candidate = Path(config.get(key, "")).resolve()
    if not (candidate / "README.md").is_file() or (candidate / ".env").exists():
        raise SystemExit("Explicit clean service worktrees are required")
if not (Path(config.get("pg_bin", "")) / "psql").is_file():
    raise SystemExit("Explicit PostgreSQL 16 binaries required")
if (not private_path.parent.is_dir() or not private_path.parent.resolve().is_absolute()
        or os.stat(private_path).st_mode & 0o777 != 0o600):
    raise SystemExit("Invalid private configuration path")

endpoints = set()
names = set()
for kind in ("ai", "conversation", "control_plane", "provider"):
    value = config["databases"][kind]
    parsed = urlsplit(value["url"])
    database = parsed.path.removeprefix("/")
    if (parsed.scheme != "postgresql+asyncpg" or parsed.hostname != "127.0.0.1"
            or parsed.port != int(config["port"]) or not parsed.username or not parsed.password
            or not database.startswith(f"handoff_e2e_{kind}_test_")):
        raise SystemExit("Only explicit numeric-loopback disposable databases are allowed")
    endpoints.add((parsed.hostname, parsed.port))
    names.add(database)
if len(names) != 4:
    raise SystemExit("Handoff E2E requires four distinct disposable databases")
network.PREIMPORT_GUARD = network.install_guard(endpoints)
network.self_check()

safe = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HANDOFF_E2E_PRIVATE_CONFIG": str(private_path.resolve()),
        "GOAL5_CS_ROOT": str(Path(config["conversation_root"]).resolve()),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
os.environ.clear()
os.environ.update(safe)

import pytest  # noqa: E402

raise SystemExit(pytest.main([
    "-q", "-p", "no:cacheprovider",
    "tests/handoff_telegram_e2e/acceptance_http.py", *sys.argv[1:],
]))
