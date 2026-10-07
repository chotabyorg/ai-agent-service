"""Opt-in real-app bootstrap for the two-process Goal2 acceptance harness.

Only external provider seams, Redis and prompt clock are fake. The authenticated
managed route, shared core, direct OpenAI HTTP client and PostgreSQL are real.
This module is never imported by production startup.
"""

import asyncio
import base64
import json
import os
import socket
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID


def install_guard(allowlist):
    endpoints = {(str(host), int(port)) for host, port in allowlist}
    if not endpoints or any(host != "127.0.0.1" for host, _ in endpoints):
        raise RuntimeError("Invalid Goal2 runtime network allowlist")
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo

    def check(address):
        if (address[0], int(address[1])) not in endpoints:
            raise RuntimeError("Goal2 runtime network destination forbidden")

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        elif sock.family == socket.AF_UNIX and address:
            raise RuntimeError("Goal2 named Unix socket forbidden")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        return original_connect_ex(sock, address)

    def resolve(host, port, *args, **kwargs):
        check((host, port))
        return original_resolve(host, port, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = resolve


class State:
    def __init__(self, directory):
        self.directory = Path(directory)

    def read(self, name):
        return json.loads((self.directory / name).read_text())

    def write(self, name, value):
        target = self.directory / name
        temporary = target.with_suffix(".new")
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.replace(target)

    def record(self, name, value=1):
        state = self.read("observations.json")
        if isinstance(value, int):
            state[name] = state.get(name, 0) + value
        else:
            state.setdefault(name, []).append(value)
        self.write("observations.json", state)


def prepare_fakes(state):
    import core.redis as redis_module
    import main
    import services.llm_service as llm_module
    import services.prompt_builder as prompt_module
    from core.managed_security import managed_logging
    from services.history_service import HistoryService
    from services.session_service import SessionService

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 6, 12, 0, 0, tzinfo=tz)

    prompt_module.datetime = FrozenDatetime

    class FileRedis:
        async def get(self, key):
            state.record("redis_get")
            return state.read("redis.json").get(key, {}).get("value")

        async def set(self, key, value, ex=None):
            state.record("redis_set")
            values = state.read("redis.json")
            values[key] = {"value": value, "ttl": ex}
            state.write("redis.json", values)

    async def redis_start():
        redis_module._redis_client = FileRedis()
        redis_module._redis_pool = None

    async def redis_stop():
        redis_module._redis_client = None

    main.init_redis = redis_start
    main.close_redis = redis_stop
    for owner, name in [(HistoryService, "load_history"), (HistoryService, "save_history"),
                        (SessionService, "get_or_create_session"),
                        (SessionService, "create_session"), (SessionService, "get_session_by_id")]:
        original = getattr(owner, name)

        def wrapper(original=original):
            async def guarded(self, *args, **kwargs):
                if managed_logging.get():
                    state.record("managed_forbidden_store")
                    raise RuntimeError("Managed Session/history method forbidden")
                return await original(self, *args, **kwargs)
            return guarded

        setattr(owner, name, wrapper())

    class FakeManager:
        def __init__(self, service_name, credentials):
            self.service_name = service_name.lower()
            self.session = self.stack = None
            self._tools = []
            if credentials.get("fixture_credential") != "synthetic-only":
                raise RuntimeError("Synthetic encrypted credentials not loaded")
            state.record("manager_init")

        async def acquire_owned(self, stack, runtime):
            runtime.managers.append(self)
            stack.push_async_callback(self.close)
            state.record("manager_enter")
            return self

        async def __aenter__(self):
            state.record("manager_enter")
            return self

        async def __aexit__(self, *args):
            await self.close()

        async def close(self):
            state.record("manager_close")
            if state.read("scenario.json").get("cleanup_error"):
                raise RuntimeError("Synthetic cleanup error")

        def get_openai_tools(self):
            return state.read("scenario.json").get("tools", {}).get(self.service_name, [])

        async def get_tools_metadata(self):
            return state.read("scenario.json").get("metadata", {}).get(self.service_name, {})

        async def call_tool(self, name, arguments):
            state.record("tool_calls", {"service": self.service_name, "name": name,
                                        "arguments": arguments})
            if name.startswith("mutate"):
                state.record("effects")
            script = state.read("scenario.json")
            if script.get("tool_exception"):
                raise RuntimeError(script["tool_exception"])
            result = script.get("results", {}).get(name, {"content": [
                {"type": "text", "text": '{"ok":true}'}], "isError": False})
            result = dict(result)
            result["content"] = [SimpleNamespace(**block) for block in result.get("content", [])]
            return SimpleNamespace(**result)

    llm_module.MCPManager = FakeManager


async def seed(config):
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from db.database import AsyncSessionLocal, engine
    from models.agent import AgentCredentials, AgentTemplate

    cipher = Fernet(os.environ["ENCRYPTION_KEY"].encode())

    def encrypt(value):
        return base64.urlsafe_b64encode(cipher.encrypt(value.encode())).decode()

    async with AsyncSessionLocal() as session:
        for record in config["agents"]:
            agent_id = UUID(record["id"])
            if await session.scalar(select(AgentTemplate.id).where(AgentTemplate.id == agent_id)):
                continue
            session.add(AgentTemplate(
                id=agent_id, user_id=UUID(record["owner"]), name="Goal2 fixture",
                system_prompt="Fixture system", prompt_config=record.get("prompt_config"),
                llm_provider="fixture", model_name="fixture-model", temperature=0.25,
                timezone="Europe/Minsk", is_active=True,
                encrypted_openrouter_key=encrypt("synthetic-model-key"),
            ))
            await session.flush()
            for family in ("bitrix", "google_workspace"):
                payload = json.dumps({"fixture_credential": "synthetic-only"})
                session.add(AgentCredentials(
                    agent_id=agent_id, service_name=family,
                    credentials=["chota:mcp-credentials", 1, encrypt(payload)],
                    enabled_tools=record.get("enabled_tools"),
                ))
        await session.commit()
    await engine.dispose()


class Observe:
    """Passive observation only: never consumes, changes or replaces a route."""

    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        managed = scope["type"] == "http" and scope["path"] == "/internal/v1/managed-turns/execute"
        if managed:
            self.state.record("managed_http_requests")
        body = bytearray()

        async def observe_send(event):
            if managed and event["type"] == "http.response.body":
                body.extend(event.get("body", b""))
                if not event.get("more_body", False):
                    value = json.loads(body)
                    if "status" in value:
                        self.state.record("managed_results", {key: value[key] for key in
                            ["turn_id", "conversation_id", "status", "error_code"]})
            await send(event)

        try:
            await self.app(scope, receive, observe_send)
        finally:
            if managed and scope.get("managed_turn_request") is not None:
                self.state.record("managed_projections", scope["managed_turn_request"].model_dump(mode="json"))


def run():
    config = json.loads(Path(os.environ["GOAL2_BOOTSTRAP_CONFIG"]).read_text())
    candidate = Path.cwd().resolve()
    if (candidate / ".env").exists():
        raise RuntimeError("Goal2 refuses candidate .env configuration")
    install_guard(config["allowlist"])
    state = State(config["state_dir"])
    for method in ["connect", "connect_ex"]:
        with socket.socket() as probe:
            try:
                getattr(probe, method)(("203.0.113.1", 443))
            except RuntimeError:
                state.record("guard_denials")
            else:
                raise RuntimeError("Goal2 runtime guard self-check failed")
    prepare_fakes(state)
    import main
    import uvicorn

    if Path(main.__file__).resolve().parent != candidate:
        raise RuntimeError("Goal2 imported wrong AI candidate")
    asyncio.run(seed(config))
    listener = socket.fromfd(config["fd"], socket.AF_INET, socket.SOCK_STREAM)
    server = uvicorn.Server(uvicorn.Config(
        Observe(main.app, state), host="127.0.0.1", port=config["port"],
        access_log=False, log_level="warning", timeout_graceful_shutdown=3,
    ))
    server.run(sockets=[listener])


if __name__ == "__main__":
    run()
