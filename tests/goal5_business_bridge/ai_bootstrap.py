"""Run the actual AI managed endpoint with only local test-owned providers."""

import asyncio
import json
import os
import socket
from dataclasses import replace
from pathlib import Path

from tests.goal5_business_bridge import network

config = json.loads(Path(os.environ["GOAL5_AI_CONFIG"]).read_text())
candidate = Path.cwd().resolve()
if candidate != Path(config["ai_root"]).resolve() or (candidate / ".env").exists():
    raise RuntimeError("Goal5 AI candidate mismatch or .env forbidden")
network.install_guard(config["allowlist"])
network.self_check()

from tests.goal2_cross_service_bootstrap import State as LegacyState  # noqa: E402
from tests.goal2_cross_service_bootstrap import prepare_fakes  # noqa: E402
from tests.goal3_business_cross_service.adapter import registry  # noqa: E402
from tests.goal3_business_cross_service.bootstrap import seed  # noqa: E402
from services.business_registry import BusinessRegistry  # noqa: E402

state = LegacyState(config["state_dir"])
prepare_fakes(state)
registrations = registry(
    config["provider_url"], native_policy="DURABLE_TEST", timeout=4,
    bounded_acceptance="PROVIDER_ACCEPT_BEFORE_V1",
)
confirmation_mode = config["profile"].get("confirmation_mode")
if confirmation_mode is not None:
    registrations = BusinessRegistry(
        replace(registration, confirmation_mode=confirmation_mode)
        for registration in registrations.values()
    )
asyncio.run(seed(config, registrations))

import main  # noqa: E402
import uvicorn  # noqa: E402
from db.database import AsyncSessionLocal  # noqa: E402

main.app.state.business_session_factory = AsyncSessionLocal
main.app.state.business_registry = registrations
listener = socket.fromfd(config["fd"], socket.AF_INET, socket.SOCK_STREAM)
server = uvicorn.Server(uvicorn.Config(
    main.app, host="127.0.0.1", port=config["port"], access_log=False,
    log_level="warning", timeout_graceful_shutdown=3,
))
server.run(sockets=[listener])
