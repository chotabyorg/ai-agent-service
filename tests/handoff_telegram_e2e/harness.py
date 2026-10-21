"""Real local service stack and deterministic User/Telegram peer fixtures."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.engine import make_url

AI_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = AI_ROOT / "tests/handoff_telegram_e2e/runtime_bootstrap.py"


def _private_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)


def _process_env(python: Path, pg_bin: Path) -> dict[str, str]:
    return {
        "PATH": f"{python.parent}:{pg_bin}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }


class _OwnedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class UserPeer:
    """Real HTTP `/api/v1/me` stand-in with two owner sessions and one foreign one."""

    def __init__(self, listener, owner_account: str, foreign_account: str):
        self.accounts = {
            "owner-a": owner_account,
            "owner-b": owner_account,
            "foreign": foreign_account,
        }
        self.calls: list[dict[str, str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != "/api/v1/me":
                    self._reply(404, {"detail": "not found"})
                    return
                cookie = self.headers.get("Cookie", "")
                token = cookie.removeprefix("access_token=").split(".", 1)[0]
                account = owner.accounts.get(token)
                owner.calls.append({"path": self.path, "session": token,
                                    "account": account or "unauthenticated"})
                if account is None:
                    self._reply(401, {"detail": "invalid session"})
                    return
                self._reply(200, {"id": account, "status": "active",
                                  "email_verified": True, "consent_required": False})

            def _reply(self, status: int, value: dict) -> None:
                body = json.dumps(value, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        self.server = _OwnedHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.socket.close()
        self.server.socket = listener
        self.server.server_address = listener.getsockname()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name="handoff-user-peer")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()


class TelegramPeer:
    """Small local Telegram Bot API peer; records writes and supports barriers."""

    def __init__(self, listener, bot_token: str, bot_id: int):
        self.bot_token = bot_token
        self.bot_id = bot_id
        self.updates: list[dict] = []
        self.update_ids: set[int] = set()
        self.send_modes: deque[str] = deque()
        self.send_attempts: list[dict] = []
        self.accepted_messages: list[dict] = []
        self.method_calls: list[str] = []
        self.send_entered = threading.Event()
        self.send_release = threading.Event()
        self.send_release.set()
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if 0 < length <= 1_048_576 else b"{}"
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    try:
                        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                        body = {key: values[-1] for key, values in form.items() if values}
                    except UnicodeDecodeError:
                        body = {}
                for name in ("chat_id", "offset"):
                    if isinstance(body.get(name), str):
                        try:
                            body[name] = int(body[name])
                        except ValueError:
                            pass
                method = self.path.rsplit("/", 1)[-1]
                with owner._lock:
                    owner.method_calls.append(method)
                if method == "getMe":
                    self._reply(200, {"ok": True, "result": {
                        "id": owner.bot_id, "is_bot": True, "first_name": "Local Handoff",
                        "username": "local_handoff_test_bot",
                    }})
                elif method == "getUpdates":
                    offset = body.get("offset")
                    with owner._lock:
                        if type(offset) is int:
                            owner.updates[:] = [item for item in owner.updates
                                                if item["update_id"] >= offset]
                        result = list(owner.updates)
                    self._reply(200, {"ok": True, "result": result})
                elif method == "sendMessage":
                    mode = "accept" if not owner.send_modes else owner.send_modes.popleft()
                    attempt = {"chat_id": body.get("chat_id"), "text": body.get("text"),
                               "mode": mode,
                               "request_started_at": datetime.now(UTC).isoformat()}
                    with owner._lock:
                        owner.send_attempts.append(attempt)
                        owner.send_entered.set()
                    if mode == "hold":
                        owner.send_release.wait(30)
                        mode = "accept"
                    if mode == "unknown":
                        # The peer accepted the write, then lost the response.
                        with owner._lock:
                            owner.accepted_messages.append({**attempt, "message_id": 82001})
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        self.connection.close()
                        return
                    if mode == "server_error":
                        self._reply(500, {"ok": False, "error_code": 500,
                                          "description": "synthetic provider uncertainty"})
                        return
                    message_id = 82000 + len(owner.accepted_messages) + 1
                    accepted = {**attempt, "message_id": message_id}
                    with owner._lock:
                        owner.accepted_messages.append(accepted)
                    self._reply(200, {"ok": True, "result": {
                        "message_id": message_id, "date": 1,
                        "chat": {"id": body.get("chat_id"), "type": "private"},
                        "text": body.get("text", ""),
                    }})
                else:
                    self._reply(200, {"ok": True, "result": True})

            def _reply(self, status: int, value: dict) -> None:
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = _OwnedHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.socket.close()
        self.server.socket = listener
        self.server.server_address = listener.getsockname()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name="handoff-telegram-peer")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def enqueue(self, update: dict) -> None:
        with self._lock:
            if update["update_id"] not in self.update_ids:
                self.update_ids.add(update["update_id"])
                self.updates.append(update)

    def arm_send(self, mode: str) -> None:
        if mode not in {"accept", "hold", "unknown", "server_error"}:
            raise ValueError("Unknown local Telegram send mode")
        with self._lock:
            self.send_modes.append(mode)
            self.send_entered.clear()
        if mode == "hold":
            self.send_release.clear()

    def release_send(self) -> None:
        self.send_release.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {"send_attempts": list(self.send_attempts),
                    "accepted_messages": list(self.accepted_messages),
                    "method_calls": list(self.method_calls)}

    def close(self) -> None:
        self.send_release.set()
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()


def _runtime_config(base, name: str, service_root: Path, allowed: set[tuple[str, int]],
                    *, telegram_api_url: str | None = None) -> Path:
    listener = base.listeners[name]
    value = {
        "fd": listener.fileno(),
        "port": int(listener.getsockname()[1]),
        "service_root": str(service_root.resolve()),
        "state_dir": str(base.root),
        "allowlist": sorted([[host, port] for host, port in allowed]),
    }
    if telegram_api_url is not None:
        value["telegram_api_url"] = telegram_api_url
    target = base.root / f"{name}-runtime.json"
    _private_json(target, value)
    return target


def _spawn(base, name: str, kind: str, python: str, root: Path,
           runtime_config: Path) -> None:
    if name in base.processes and base.processes[name].poll() is None:
        return
    env = _process_env(Path(python), Path(base.config["pg_bin"]).resolve())
    inherited = "cs" if name == "worker" else "telegram" if name == "tg" else name
    env.update(base.envs.get(inherited, {}))
    env["HANDOFF_E2E_RUNTIME_CONFIG"] = str(runtime_config)
    log_path = base.root / f"handoff-{name}.log"
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    handle = os.fdopen(descriptor, "wb")
    base.handles[name] = handle
    listener = base.listeners[name]
    base.processes[name] = subprocess.Popen(
        [python, str(RUNTIME), kind], cwd=root, env=env,
        stdout=handle, stderr=handle, pass_fds=(listener.fileno(),),
        start_new_session=True,
    )


async def _ready(base, name: str, *, path: str = "/health", timeout: float = 25) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    url_name = {"cp": "control_plane", "worker": "conversation_worker"}.get(name, name)
    while asyncio.get_running_loop().time() < deadline:
        process = base.processes[name]
        if process.poll() is not None:
            raise AssertionError(f"Owned {name} process exited; inspect its private log")
        try:
            response = await base.client.get(base.url(url_name) + path, timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"Owned {name} process readiness timed out")


async def _seed_control_plane(base, config: dict) -> None:
    from sqlalchemy import text

    owner = base.grant["owner_id"]
    integration_id = base.ids["integration"]
    agent_id = base.ids["agent"]
    token = base.tokens["bot"]
    encryption_key = base.envs["ai"]["ENCRYPTION_KEY"]
    encrypted = Fernet(encryption_key.encode()).encrypt(
        json.dumps({"BOT_TOKEN": token}, separators=(",", ":")).encode()
    )
    credentials = {"encrypted": base64.urlsafe_b64encode(encrypted).decode()}
    async with base.engines["control_plane"].begin() as connection:
        await connection.execute(text(
            "INSERT INTO integrations(id,agent_id,owner_id,channel_type,credentials,"
            "is_active,status) VALUES(:id,:agent,:owner,'TELEGRAM',CAST(:credentials AS jsonb),"
            "true,'ACTIVE')"
        ), {"id": integration_id, "agent": agent_id, "owner": owner,
            "credentials": json.dumps(credentials)})
        await connection.execute(text(
            "INSERT INTO inbox_integration_bindings(integration_id,owner_id,agent_id,"
            "binding_generation,grant_generation,grant_active,state) VALUES(:id,:owner,:agent,"
            "1,0,false,'READY')"
        ), {"id": integration_id, "owner": owner, "agent": agent_id})


async def configure_handoff_services(base, config: dict) -> None:
    """Restart AI and Conversation under exact handoff scope/worker credentials."""
    from tests.goal5_business_bridge.harness import private_json
    from sqlalchemy import text

    owner = base.grant["owner_id"]
    integration = base.ids["integration"]
    agent = base.ids["agent"]
    account = base.ids["agent_owner"]
    cp_token, ai_lifecycle, enrollment = (
        secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    )
    base.tokens.update(cp_lifecycle=cp_token, ai_lifecycle=ai_lifecycle,
                       enrollment=enrollment, bot=secrets.token_urlsafe(16))
    base.ids.update(account=account, foreign_account=str(uuid4()), bot_id="731005")
    base.tokens["bot"] = f"731005:{secrets.token_urlsafe(28)}"

    # Goal5's generic paired fixture creates an active agent without exercising
    # Handoff lifecycle enrollment. Seed the valid first lifecycle generation
    # that a real active, enrollable agent already has in production.
    async with base.engines["ai"].begin() as connection:
        seeded = await connection.execute(text(
            "UPDATE agent_templates SET handoff_lifecycle_generation=1 "
            "WHERE id=:agent_id AND is_active=true AND handoff_lifecycle_state='STABLE'"
        ), {"agent_id": agent})
        if seeded.rowcount != 1:
            raise AssertionError("Owned active Handoff test agent fixture is missing")

    base.user_peer = UserPeer(base.listeners["user"], account, base.ids["foreign_account"])
    base.user_peer.start()
    base.telegram_peer = TelegramPeer(base.listeners["telegram_api"], base.tokens["bot"],
                                      int(base.ids["bot_id"]))
    base.telegram_peer.start()

    ai_scopes = [{"owner_id": owner, "integration_id": integration,
                  "channel": "TELEGRAM", "agent_id": agent}]
    base.envs["ai"].update(
        HANDOFF_AI_ENROLLED_SCOPES=json.dumps(ai_scopes),
        HANDOFF_CONVERSATION_URL=base.url("cs"),
        HANDOFF_LIFECYCLE_TOKEN=ai_lifecycle,
    )
    await base.restart("ai")

    principal_values = json.loads(base.envs["cs"]["CONVERSATION_AUTH_PRINCIPALS"])
    telegram_grant = {"owner_id": owner, "integration_id": integration,
                      "channel": "TELEGRAM", "provider_namespace": "main-chota.telegram.v1"}
    for principal_id, role in (("handoff-telegram-gateway", "TELEGRAM"),
                               ("handoff-telegram-worker", "TELEGRAM_PROCESSING")):
        principal_values.append({"principal_id": principal_id, "role": role,
                                 "token": secrets.token_urlsafe(32),
                                 "grants": [telegram_grant]})
    gateway_token = principal_values[-2]["token"]
    worker_token = principal_values[-1]["token"]
    base.tokens["telegram_gateway"] = gateway_token
    base.tokens["telegram_worker"] = worker_token
    bindings = json.loads(base.envs["cs"]["MANAGED_AI_BINDINGS"])
    base.envs["cs"].update(
        CONVERSATION_AUTH_PRINCIPALS=json.dumps(principal_values),
        TELEGRAM_CANARY_INTEGRATION_IDS=json.dumps([integration]),
        TELEGRAM_HANDOFF_INTEGRATION_IDS=json.dumps([integration]),
        TELEGRAM_WORKER_ENABLED="true",
        TELEGRAM_WORKER_PRINCIPAL_ID="handoff-telegram-worker",
        TELEGRAM_WORKER_POLL_SECONDS="0.1",
        TELEGRAM_WORKER_HEALTH_PORT=str(base.listeners["conversation_worker"].getsockname()[1]),
        TELEGRAM_MANAGED_BINDINGS=json.dumps([{
            "owner_id": owner, "integration_id": integration, "channel": "TELEGRAM",
            "agent_id": agent, "service_token": worker_token,
        }]),
        USER_SERVICE_BASE_URL=base.user_peer.base_url,
        HANDOFF_CP_BASE_URL=base.url("control_plane"),
        HANDOFF_AI_BASE_URL=base.url("ai"),
        INBOX_LIFECYCLE_TOKEN=cp_token,
        HANDOFF_LIFECYCLE_TOKEN=ai_lifecycle,
    )
    # The managed Conversation worker has its own TELEGRAM_PROCESSING principal;
    # preserve the existing API-level service principal for enrollment proofs.
    worker_cfg_path = Path(base.envs["cs"]["GOAL2_BOOTSTRAP_CONFIG"])
    worker_cfg = json.loads(worker_cfg_path.read_text())
    cs_db = make_url(base.urls["conversation"])
    proxy_port = base.listeners["ai_proxy"].getsockname()[1]
    allow = {
        ("127.0.0.1", int(cs_db.port)), ("127.0.0.1", int(proxy_port)),
        ("127.0.0.1", base.listeners["user"].getsockname()[1]),
        ("127.0.0.1", base.listeners["control_plane"].getsockname()[1]),
        ("127.0.0.1", base.listeners["ai"].getsockname()[1]),
    }
    worker_cfg["allowlist"] = sorted([[host, port] for host, port in allow])
    private_json(base.root / "cs-config.json", worker_cfg)
    base.envs["cs"]["GOAL2_BOOTSTRAP_CONFIG"] = str(base.root / "cs-config.json")
    await base.restart("cs")

    cp_database = make_url(base.urls["control_plane"])
    cp_env = _process_env(Path(config["control_plane_python"]), Path(config["pg_bin"]))
    cp_env.update(
        POSTGRES_HOST="127.0.0.1", POSTGRES_PORT=str(cp_database.port),
        POSTGRES_DB=cp_database.database, POSTGRES_USER=cp_database.username,
        POSTGRES_PASSWORD=cp_database.password, ENCRYPTION_KEY=base.envs["ai"]["ENCRYPTION_KEY"],
        INTERNAL_API_TOKEN=secrets.token_urlsafe(32), INBOX_LIFECYCLE_TOKEN=cp_token,
        INBOX_ENROLLMENT_TOKEN=enrollment, CONVERSATION_SERVICE_URL=base.url("cs"),
        INBOX_LIFECYCLE_TIMEOUT_SECONDS="5", LOG_LEVEL="WARNING",
    )
    base.tokens["cp_internal"] = cp_env["INTERNAL_API_TOKEN"]
    base.envs["cp"] = cp_env
    cp_allow = {("127.0.0.1", int(cp_database.port)),
                ("127.0.0.1", base.listeners["cs"].getsockname()[1])}
    cp_config = _runtime_config(base, "control_plane", Path(config["control_plane_root"]), cp_allow)
    base.runtime_configs = {"cp": cp_config}
    base.listeners["cp"] = base.listeners["control_plane"]
    _spawn(base, "cp", "control-plane", config["control_plane_python"],
           Path(config["control_plane_root"]), cp_config)
    await _ready(base, "cp")
    await _seed_control_plane(base, config)

    enroll_body = {"contract_version": 1, "command_id": str(uuid4()), "account_id": account}
    enrolled = await base.client.post(
        base.url("cp") + f"/internal/v1/handoff/integrations/{integration}/enroll",
        json=enroll_body, headers={"X-Inbox-Enrollment-Token": enrollment}, timeout=10,
    )
    if enrolled.status_code != 200:
        detail = enrolled.json().get("detail") if enrolled.headers.get("content-type", "").startswith(
            "application/json"
        ) else None
        if not isinstance(detail, str) or len(detail) > 120:
            detail = "no safe service detail"
        raise AssertionError(f"Owned CP enrollment failed ({enrolled.status_code}): {detail}")
    base.enrollment = enrolled.json()

    active = await base.client.get(
        base.url("cp") + "/v1/internal/integrations",
        params={"channel_type": "TELEGRAM"},
        headers={"X-Internal-Token": base.tokens["cp_internal"]},
    )
    if (active.status_code != 200 or not any(
        item.get("integration_id") == integration for item in active.json()
    )):
        raise AssertionError("Owned CP did not expose the enrolled active Telegram integration")

    worker_cfg_path = _runtime_config(
        base, "conversation_worker", Path(config["conversation_root"]),
        {("127.0.0.1", int(cs_db.port)), ("127.0.0.1", int(proxy_port))},
    )
    base.runtime_configs["worker"] = worker_cfg_path
    base.listeners["worker"] = base.listeners["conversation_worker"]
    _spawn(base, "worker", "conversation-worker", config["conversation_python"],
           Path(config["conversation_root"]), worker_cfg_path)
    await _ready(base, "worker", path="/ready")

    tg_env = _process_env(Path(config["telegram_python"]), Path(config["pg_bin"]))
    tg_env.update(
        AI_AGENT_SERVICE_URL=base.url("ai"), CONTROL_PLANE_URL=base.url("cp"),
        INTERNAL_API_TOKEN=base.tokens["cp_internal"],
        CHAT_S2S_TOKEN=secrets.token_urlsafe(32),
        CONVERSATION_SERVICE_URL=base.url("cs"), CONVERSATION_SERVICE_TOKEN=gateway_token,
        TELEGRAM_MANAGED_BINDINGS=json.dumps([{
            "owner_id": owner, "integration_id": integration, "agent_id": agent,
        }]),
        TELEGRAM_CANARY_INTEGRATION_IDS=json.dumps([integration]),
        TELEGRAM_HANDOFF_INTEGRATION_IDS=json.dumps([integration]),
        INTEGRATIONS_POLL_INTERVAL="1",
        HEALTH_PORT=str(base.listeners["tg_gateway"].getsockname()[1]), LOG_LEVEL="INFO",
    )
    base.envs["telegram"] = tg_env
    telegram_allow = {
        ("127.0.0.1", base.listeners["cp"].getsockname()[1]),
        ("127.0.0.1", base.listeners["cs"].getsockname()[1]),
        ("127.0.0.1", base.listeners["telegram_api"].getsockname()[1]),
    }
    tg_config = _runtime_config(
        base, "tg_gateway", Path(config["telegram_root"]), telegram_allow,
        telegram_api_url=base.telegram_peer.base_url,
    )
    base.runtime_configs["telegram"] = tg_config
    base.listeners["tg"] = base.listeners["tg_gateway"]
    _spawn(base, "tg", "telegram", config["telegram_python"],
           Path(config["telegram_root"]), tg_config)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if base.processes["tg"].poll() is not None:
            raise AssertionError("Owned Telegram gateway exited; inspect its private log")
        if "getUpdates" in base.telegram_peer.snapshot()["method_calls"]:
            break
        await asyncio.sleep(0.05)
    else:
        methods = base.telegram_peer.snapshot()["method_calls"][-10:]
        raise AssertionError(
            f"Owned Telegram gateway did not start polling the local Bot API peer; methods={methods!r}"
        )


async def start_handoff_services(base, config: dict) -> None:
    # At this point AI and Conversation have been restarted with the Handoff scope.
    # Control Plane enrollment wires both live proofs before Telegram starts.
    await _configure_and_enroll(base, config)


async def _configure_and_enroll(base, config: dict) -> None:
    await configure_handoff_services(base, config)


async def close_handoff_services(base) -> None:
    for name in ("tg", "worker", "cp"):
        process = base.processes.get(name)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 8)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 3)
    for peer_name in ("telegram_peer", "user_peer"):
        peer = getattr(base, peer_name, None)
        if peer is not None:
            peer.close()
