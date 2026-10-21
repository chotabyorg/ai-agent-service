"""Start owned Control Plane, Conversation worker, or Telegram processes."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AI_ROOT))

from tests.goal5_business_bridge.network import install_guard, self_check  # noqa: E402


def _runtime_config() -> dict:
    path = Path(os.environ["HANDOFF_E2E_RUNTIME_CONFIG"])
    if path.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError("Private runtime configuration must have mode 0600")
    value = json.loads(path.read_text())
    install_guard(value["allowlist"])
    self_check()
    return value


def _serve(app, config: dict) -> None:
    import uvicorn

    descriptor = int(config["fd"])
    listener = socket.fromfd(descriptor, socket.AF_INET, socket.SOCK_STREAM)
    uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=int(config["port"]), access_log=False,
        log_level="warning", timeout_graceful_shutdown=5,
    )).run(sockets=[listener])


def run_control_plane(config: dict) -> None:
    sys.path.insert(0, str(Path(config["service_root"]).resolve()))
    import main

    _serve(main.app, config)


def run_conversation_worker(config: dict) -> None:
    sys.path.insert(0, str(Path(config["service_root"]).resolve()))
    from core.config import load_settings
    from telegram_worker import create_worker_app

    _serve(create_worker_app(load_settings()), config)


def run_telegram(config: dict) -> None:
    sys.path.insert(0, str(Path(config["service_root"]).resolve()))
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    import services.bot_manager as bot_manager
    import services.health_server as health_server

    api = TelegramAPIServer.from_base(config["telegram_api_url"])

    def local_bot(*, token: str, default: object) -> Bot:
        return Bot(token=token, session=AiohttpSession(api=api), default=default)

    bot_manager.Bot = local_bot  # type: ignore[assignment]

    class HealthRunner:
        async def cleanup(self) -> None:
            return None

    async def local_health(*_args, **_kwargs):
        return HealthRunner()

    health_server.start_health_server = local_health
    from main import main

    asyncio.run(main())


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected runtime kind")
    config = _runtime_config()
    kind = sys.argv[1]
    if kind == "control-plane":
        run_control_plane(config)
    elif kind == "conversation-worker":
        run_conversation_worker(config)
    elif kind == "telegram":
        run_telegram(config)
    else:
        raise SystemExit("unknown runtime kind")


if __name__ == "__main__":
    main()
