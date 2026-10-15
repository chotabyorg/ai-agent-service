"""Run the existing SQL-durable fake provider with a per-run PG allowlist."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from tests.goal5_business_bridge import network

config_path = Path(os.environ["GOAL3_PROVIDER_CONFIG"])
config = json.loads(config_path.read_text())
port = urlsplit(config["database"]).port
if not port:
    raise RuntimeError("Fake provider database must have an explicit port")
allowed = network.install_guard([("127.0.0.1", int(port))])
network.self_check()

from tests.goal3_business_cross_service import fake_provider  # noqa: E402

fake_provider.install_guard = lambda _endpoints: allowed
fake_provider.self_check = network.self_check
fake_provider.run()
