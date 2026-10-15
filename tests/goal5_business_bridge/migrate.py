"""Guarded native Alembic child for explicit Goal5 test databases."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.goal5_business_bridge.network import install_guard, self_check

config = json.loads(Path(os.environ["GOAL5_MIGRATION_CONFIG"]).read_text())
install_guard(config["allowlist"])
self_check()
service_root = Path(config["service_root"]).resolve()
os.chdir(service_root)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

command.upgrade(Config(str(service_root / "alembic.ini")), "head")
