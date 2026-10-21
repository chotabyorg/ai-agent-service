"""Guarded native Alembic runner for one explicitly named test database."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AI_ROOT))

from tests.goal5_business_bridge.network import install_guard, self_check  # noqa: E402

config = json.loads(Path(os.environ["HANDOFF_E2E_MIGRATION_CONFIG"]).read_text())
install_guard(config["allowlist"])
self_check()
service_root = Path(config["service_root"]).resolve()
if (service_root / ".env").exists():
    raise RuntimeError("Candidate .env configuration is forbidden")
os.chdir(service_root)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

command.upgrade(Config(str(service_root / "alembic.ini")), "head")
