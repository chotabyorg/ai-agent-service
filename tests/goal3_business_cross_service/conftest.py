"""Explicit disposable inputs and independent owned HTTP process fixtures."""

import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.goal3_business_cross_service.network import install_guard, self_check
from tests.goal3_business_cross_service.state import State, private_json

AI_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/internal/v1"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "rep_" + report.when, report)


@pytest.fixture(scope="session")
def paired_inputs():
    path = Path(os.environ.get("GOAL3_PRIVATE_CONFIG", ""))
    if not path.is_absolute() or not path.is_file():
        pytest.fail("Explicit private Goal3 paired configuration required")
    if path.stat().st_mode & 0o777 != 0o600:
        pytest.fail("Private Goal3 paired configuration must have mode 0600")
    value = json.loads(path.read_text())
    if value.get("disposable") != "yes" or Path(value.get("ai_root", "")) != AI_ROOT:
        pytest.fail("Exact AI candidate and explicit disposable acknowledgement required")
    cs_root = Path(value.get("cs_root", ""))
    for key in ("ai_python", "cs_python", "cs_root"):
        if not Path(value.get(key, "")).is_absolute() or not Path(value[key]).exists():
            pytest.fail("Explicit absolute paired candidate runtimes required")
    if ((AI_ROOT / ".env").exists() or (cs_root / ".env").exists()
            or not (cs_root / "tests/managed_cross_service/runtime_bootstrap.py").is_file()):
        pytest.fail("Checked Conversation bootstrap required; .env forbidden")
    names = set()
    for key in ("ai", "cs", "provider"):
        url = make_url(value.get("databases", {}).get(key, ""))
        if (url.drivername != "postgresql+asyncpg" or url.host != "127.0.0.1"
                or url.port != 55438 or not url.database
                or "goal3" not in url.database or "test" not in url.database
                or not url.username or not url.password):
            pytest.fail("Only explicit task-owned numeric-loopback Goal3 PostgreSQL16 test DBs allowed")
        names.add(url.database)
    if len(names) != 3:
        pytest.fail("Three distinct disposable service/provider databases required")
    return value


@pytest.fixture(scope="session")
def postgres_url(paired_inputs):
    # Override the ordinary AI testcontainer fixture only in this explicit package.
    return paired_inputs["databases"]["ai"]


def clean_env():
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}


@pytest.fixture(scope="session")
def paired_migrated(paired_inputs, tmp_path_factory):
    for key, root_key, python_key in (("ai", "ai_root", "ai_python"), ("cs", "cs_root", "cs_python")):
        url = make_url(paired_inputs["databases"][key])
        env = clean_env()
        if key == "ai":
            env.update(POSTGRES_HOST=url.host, POSTGRES_PORT=str(url.port), POSTGRES_DB=url.database,
                       POSTGRES_USER=url.username, POSTGRES_PASSWORD=url.password, POSTGRES_ECHO="false")
        else:
            env["DATABASE_URL"] = paired_inputs["databases"][key]
        log_path = tmp_path_factory.mktemp("goal3-migrations") / (key + ".log")
        with log_path.open("wb") as log:
            result = subprocess.run([paired_inputs[python_key], str(AI_ROOT / "tests/goal3_business_cross_service/migrate.py")],
                                    cwd=paired_inputs[root_key], env=env, stdout=log, stderr=log, timeout=60)
        log_path.chmod(0o600)
        assert result.returncode == 0, "Real paired migration failed; private log retained"
    return paired_inputs


async def observed_waiters(engine, blocker_pid, label, count=1):
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        async with engine.connect() as observer:
            observer_pid = await observer.scalar(text("SELECT pg_backend_pid()"))
            rows = (await observer.execute(text("SELECT pid,wait_event_type,wait_event,xact_start,"
                "pg_blocking_pids(pid) AS blockers FROM pg_stat_activity WHERE datname=current_database() "
                "AND wait_event_type='Lock'"))).mappings().all()
        # PostgreSQL can queue a second row waiter behind the first worker.
        # Prove its real dependency path to the owned holder, including soft
        # queued blockers; neither unrelated waits nor scheduling count.
        connected, workers = {blocker_pid}, []
        changed = True
        while changed:
            changed = False
            for row in rows:
                if row["pid"] not in connected | {observer_pid} and connected.intersection(row["blockers"]):
                    connected.add(row["pid"])
                    workers.append(dict(row))
                    changed = True
        if len(workers) >= count:
            assert all(r["xact_start"] is not None for r in workers)
            return {"scenario": label, "blocker_pid": blocker_pid, "observer_pid": observer_pid,
                    "workers": workers}
        await asyncio.sleep(.015)
    pytest.fail("Real PostgreSQL waiter not observed: " + label)


@dataclass
class Harness:
    config: dict = field(repr=False)
    root: Path
    sql: object = field(repr=False)
    ai_sql: object = field(repr=False)
    provider_sql: object = field(repr=False)
    client: object = field(repr=False)
    listeners: dict = field(repr=False)
    tokens: dict = field(repr=False)
    ids: dict
    grants: dict
    envs: dict = field(default_factory=dict, repr=False)
    processes: dict = field(default_factory=dict, repr=False)
    handles: list = field(default_factory=list, repr=False)
    lock_evidence: list = field(default_factory=list)

    def url(self, name):
        return "http://127.0.0.1:" + str(self.listeners[name].getsockname()[1])

    async def start(self, name):
        module = {"ai": "tests.goal3_business_cross_service.bootstrap", "cs":
                  "tests.managed_cross_service.runtime_bootstrap", "provider":
                  "tests.goal3_business_cross_service.fake_provider"}[name]
        python = self.config["cs_python"] if name == "cs" else self.config["ai_python"]
        cwd = self.config["cs_root"] if name == "cs" else self.config["ai_root"]
        log = (self.root / (name + "-" + str(len(self.handles)) + ".log")).open("wb")
        self.handles.append(log)
        self.processes[name] = subprocess.Popen([python, "-m", module], cwd=cwd,
            env=self.envs[name], stdout=log, stderr=log, pass_fds=(self.listeners[name].fileno(),),
            start_new_session=True)
        private_json(self.root / "owned-processes.json", {
            key: value.pid for key, value in self.processes.items()})
        State(self.root).append("process", {"service": name, "event": "start", "pid": self.processes[name].pid})
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            assert self.processes[name].poll() is None, "Owned child exited; private log retained"
            try:
                response = await self.client.get(self.url(name) + "/health", timeout=.3)
                if response.status_code == 200:
                    if name == "cs":
                        assert (await self.client.get(self.url(name) + "/ready")).status_code == 200
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.03)
        pytest.fail("Owned child readiness deadline")

    async def stop(self, name, abrupt=False):
        process = self.processes.get(name)
        if process is None or process.poll() is not None:
            return
        process.kill() if abrupt else process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 3)
        State(self.root).append("process", {"service": name, "event": "stop", "pid": process.pid,
                                          "abrupt": abrupt, "exit_code": process.returncode})

    async def restart(self, name, abrupt=False):
        await self.stop(name, abrupt)
        await self.start(name)

    async def script(self, *, model=(), settings=None, release=(), replace_model=False):
        response = await self.client.post(self.url("provider") + "/__test__/script",
            json={"model": list(model), "settings": settings or {}, "release": list(release),
                  "clear_model": replace_model})
        assert response.status_code == 200

    async def cs(self, method, path, value=None, principal="a"):
        return await self.client.request(method, self.url("cs") + PREFIX + path,
            json=value, headers={"X-Service-Token": self.tokens[principal]})

    async def selected(self, text_value="Запиши меня на 12:00", thread=None, principal="a"):
        accepted = await self.cs("POST", "/inbound-events", dict(self.grants[principal],
            provider_thread_id=thread or str(uuid4()), provider_event_id=str(uuid4()), text=text_value), principal)
        assert accepted.status_code == 201
        value = accepted.json()
        conversation = value["conversation"]
        created = await self.cs("POST", "/conversations/" + conversation["id"] + "/turns", {
            "selection_key": str(uuid4()), "input_first_sequence": conversation["last_message_sequence"],
            "input_last_sequence": conversation["last_message_sequence"],
            "expected_input_revision": conversation["input_revision"],
            "expected_authority_revision": conversation["authority_revision"]}, principal)
        assert created.status_code == 201
        return value, await self.claim(created.json()["turn"], principal)

    async def claim(self, turn, principal="a"):
        response = await self.cs("POST", "/turns/" + turn["id"] + "/claim",
                                 {"worker_ref": "goal3-harness", "lease_seconds": 300}, principal)
        assert response.status_code == 200
        return response.json()

    @staticmethod
    def proof(claim):
        return {"claim_epoch": claim["turn"]["claim_epoch"], "claim_token": claim["claim_token"]}

    async def execute(self, claim, principal="a"):
        return await self.cs("POST", "/turns/" + claim["turn"]["id"] + "/execute",
                             self.proof(claim), principal)

    def trace(self, kind=None):
        path = self.root / "trace.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r["value"] for r in rows if kind is None or r["kind"] == kind]

    async def projection(self, turn_id):
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            found = [v for v in self.trace("projection") if v["turn_id"] == turn_id]
            if found:
                return found[-1]
            await asyncio.sleep(.02)
        pytest.fail("Actual managed projection was not observed")

    async def ai(self, path, value, token="managed"):
        return await self.client.post(self.url("ai") + PREFIX + path, json=value,
                                      headers={"X-Service-Token": self.tokens[token]})

    async def worker(self, request, action, **kwargs):
        response = await self.client.post(self.url("ai") + "/__goal3__/worker",
            json={"request": request, "action": action, **kwargs},
            headers={"X-Goal3-Worker": self.tokens["worker"]}, timeout=35)
        assert response.status_code == 200, "Actual test worker failed; private log retained"
        return response.json()

    async def operations(self, turn_id=None):
        async with self.ai_sql.connect() as db:
            where = " WHERE origin_turn_id=:id" if turn_id else ""
            return [dict(v) for v in (await db.execute(text("SELECT * FROM business_operations" +
                where + " ORDER BY ordinal,id"), {"id": turn_id} if turn_id else {})).mappings().all()]

    async def counters(self):
        async with self.provider_sql.connect() as db:
            requests = (await db.execute(text("SELECT kind,count(*) FROM fake_requests GROUP BY kind"))).all()
            objects = await db.scalar(text("SELECT count(*) FROM fake_objects"))
            posts = [dict(v) for v in (await db.execute(text("SELECT operation_id,body FROM "
                "fake_requests WHERE kind='POST' ORDER BY sequence"))).mappings().all()]
        return {"counts": dict(requests), "objects": objects, "posts": posts}

    async def phase(self, name):
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            if (self.root / ("entered-" + name + ".json")).exists():
                return
            await asyncio.sleep(.02)
        pytest.fail("Positive-commit phase not observed: " + name)

    def fault(self, phase=None):
        private_json(self.root / "faults.json", {"phase": phase})

    async def provider_barrier(self, label):
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            async with self.provider_sql.connect() as db:
                entered = await db.scalar(text("SELECT entered FROM fake_barriers WHERE label=:label"),
                                          {"label": label})
            if entered:
                return
            await asyncio.sleep(.02)
        pytest.fail("Actual provider request barrier not entered")

    async def provider_peer_eof(self, label):
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            async with self.provider_sql.connect() as db:
                observed = await db.scalar(text("SELECT c.peer_eof_at FROM fake_connections c JOIN fake_barriers b "
                    "ON b.connection_id=c.id WHERE b.label=:label"), {"label": label})
            if observed is not None:
                return
            await asyncio.sleep(.02)
        pytest.fail("Actual provider socket peer EOF was not observed")

    async def expire_turn(self, turn_id):
        async with self.sql.begin() as db:
            await db.execute(text("UPDATE agent_turns SET lease_expires_at=clock_timestamp()-interval "
                                  "'1 second' WHERE id=:id"), {"id": turn_id})

    async def expire_attempt(self, operation_id):
        # Isolated test fault seam; real-time lease expiry is a separate acceptance.
        async with self.ai_sql.begin() as db:
            await db.execute(text("UPDATE business_operations SET dispatch_lease_expires_at="
                                  "clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": operation_id})

    async def separated(self, conversation_id, expected_messages=1):
        async with self.sql.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM messages WHERE conversation_id=:id"),
                                   {"id": conversation_id}) == expected_messages
            assert await db.scalar(text("SELECT count(*) FROM delivery_attempts")) == 0
            assert await db.scalar(text("SELECT count(*) FROM delivery_reconciliations")) == 0
            tables = await db.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                                          "AND tablename LIKE 'business_%'"))
            assert tables == 0
        async with self.ai_sql.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM conversation_sessions")) == 0
        observations = json.loads((self.root / "observations.json").read_text())
        assert observations.get("managed_forbidden_store", 0) == 0
        assert observations.get("manager_init", 0) == 0
        assert observations.get("redis_get", 0) == observations.get("redis_set", 0) == 0


@pytest_asyncio.fixture
async def cross(paired_migrated, tmp_path, request):
    config = paired_migrated
    engines = {key: create_async_engine(url, poolclass=NullPool) for key, url in config["databases"].items()}
    for key, engine in engines.items():
        async with engine.begin() as db:
            assert 160000 <= int(await db.scalar(text("SHOW server_version_num"))) < 170000
            if key != "provider":
                tables = (await db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                                                "AND tablename!='alembic_version'"))).scalars().all()
                assert all(name.replace("_", "").isalnum() for name in tables)
                await db.execute(text("TRUNCATE " + ",".join('"' + name + '"' for name in tables) + " CASCADE"))
    listeners = {}
    for name in ("cs", "ai", "provider"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0)); sock.listen(64)
        listeners[name] = sock
    from tests.goal3_business_cross_service import network
    previous_allowed = set(network.PREIMPORT_GUARD) if network.PREIMPORT_GUARD is not None else None
    allowed = network.PREIMPORT_GUARD if previous_allowed is not None else install_guard([("127.0.0.1", 55438)])
    allowed.update(s.getsockname() for s in listeners.values())
    assert self_check() == 3
    tokens = {key: secrets.token_urlsafe(32) for key in ("a", "b", "managed", "worker", "chat", "internal")}
    ids = {key: str(uuid4()) for key in ("agent_a", "agent_b", "owner_a", "owner_b", "integration_a", "integration_b",
                                       "connection_a", "connection_b")}
    grants = {name: {"owner_id": "opaque-goal3-" + name, "integration_id": ids["integration_" + name],
                    "channel": "TELEGRAM", "provider_namespace": "goal3.fixture"} for name in ("a", "b")}
    for name, value in (("observations.json", {}), ("scenario.json", {}), ("redis.json", {}), ("faults.json", {})):
        private_json(tmp_path / name, value)
    async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
        h = Harness(config, tmp_path, engines["cs"], engines["ai"], engines["provider"], client,
                    listeners, tokens, ids, grants)
        mode = getattr(request, "param", "LOOKUP")
        mode = mode if isinstance(mode, dict) else {"mode": mode}
        principal_grants = [{**{k: v for k, v in grants[name].items() if k != "provider_namespace"},
                             "agent_id": ids["agent_" + name], "agent_owner_id": ids["owner_" + name]}
                            for name in ("a", "b")]
        base = clean_env()
        provider_cfg = {"fd": listeners["provider"].fileno(), "port": listeners["provider"].getsockname()[1],
                        "database": config["databases"]["provider"].replace("postgresql+asyncpg", "postgresql")}
        private_json(tmp_path / "provider.json", provider_cfg)
        h.envs["provider"] = dict(base, GOAL3_PROVIDER_CONFIG=str(tmp_path / "provider.json"))
        cs_config = {"fd": listeners["cs"].fileno(), "port": listeners["cs"].getsockname()[1],
                     "state_dir": str(tmp_path), "allowlist": [["127.0.0.1", 55438],
                                                               list(listeners["ai"].getsockname())]}
        private_json(tmp_path / "cs.json", cs_config)
        h.envs["cs"] = dict(base, DATABASE_URL=config["databases"]["cs"], LOG_LEVEL="WARNING",
            GOAL2_BOOTSTRAP_CONFIG=str(tmp_path / "cs.json"), MANAGED_AI_BASE_URL=h.url("ai"),
            MANAGED_AI_TOTAL_SECONDS="30", MANAGED_AI_READ_SECONDS="30",
            CONVERSATION_AUTH_PRINCIPALS=json.dumps([{"principal_id": name, "token": tokens[name],
                                                      "grants": [grants[name]]} for name in ("a", "b")]),
            MANAGED_AI_BINDINGS=json.dumps([{**{k: v for k, v in grant.items() if k != "agent_owner_id"},
                                             "service_token": tokens["managed"]} for grant in principal_grants]))
        ai_url = make_url(config["databases"]["ai"])
        ai_config = {"fd": listeners["ai"].fileno(), "port": listeners["ai"].getsockname()[1],
            "state_dir": str(tmp_path), "provider_url": h.url("provider"), "worker_token": tokens["worker"],
            "allowlist": [["127.0.0.1", 55438], list(listeners["provider"].getsockname())],
            "grants": principal_grants, "profile": mode,
            "agents": [{"id": ids["agent_" + name], "owner": ids["owner_" + name],
                        "connection_id": ids["connection_" + name]} for name in ("a", "b")]}
        private_json(tmp_path / "ai.json", ai_config)
        h.envs["ai"] = dict(base, POSTGRES_HOST="127.0.0.1", POSTGRES_PORT="55438", POSTGRES_DB=ai_url.database,
            POSTGRES_USER=ai_url.username, POSTGRES_PASSWORD=ai_url.password, POSTGRES_ECHO="false",
            ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), REDIS_HOST="", DEBUG="false",
            GOAL3_BOOTSTRAP_CONFIG=str(tmp_path / "ai.json"), MANAGED_AI_EXECUTION_SECONDS="25",
            MANAGED_AI_AUTH_PRINCIPALS=json.dumps([{"principal_id": "goal3-conversation", "token": tokens["managed"],
                "caller": "conversation-service", "execution_namespace": "main-chota.conversation.v1", "grants": principal_grants}]),
            CHAT_S2S_TOKEN=tokens["chat"], CHAT_S2S_AUTH_REQUIRED="true", INTERNAL_API_TOKEN=tokens["internal"],
            LLM_BASE_URL=h.url("provider") + "/v1", KNOWLEDGE_BASE_ENABLED="false")
        try:
            await h.start("provider")
            async with h.provider_sql.begin() as db:
                await db.execute(text("TRUNCATE fake_requests,fake_receipts,fake_objects,fake_settings,"
                                      "fake_model_queue,fake_barriers CASCADE"))
            await h.start("ai"); await h.start("cs")
            yield h
        finally:
            try:
                if h.processes.get("provider") is not None:
                    counters = await h.counters()
                    operations = await h.operations()
                    async with h.provider_sql.connect() as db:
                        peers = (await db.execute(text("SELECT b.label,c.peer_eof_at FROM fake_barriers b JOIN "
                            "fake_connections c ON c.id=b.connection_id WHERE c.peer_eof_at IS NOT NULL"))).mappings().all()
                    safe = {"case": request.node.name, "result": getattr(getattr(request.node, "rep_call", None),
                        "outcome", "setup_failed"), "counts": counters["counts"], "objects": counters["objects"],
                        "posts": [{"operation_id": p["operation_id"], "native_key": p["body"].get(
                            "provider_idempotency_key"), "issued_at": p["body"]["issued_at"],
                            "fingerprint": p["body"]["request_fingerprint"]} for p in counters["posts"]],
                        "operations": [{key: str(o[key]) if key in {"id", "origin_turn_id"} else o[key]
                            for key in ("id", "origin_turn_id", "status", "attempt_count", "receipt_id", "object_id")}
                            for o in operations], "locks": h.lock_evidence, "guard_denials": h.trace("guard"),
                            "process_events": h.trace("process"), "phase_signals": [p.stem for p in tmp_path.glob("entered-*.json")],
                            "actual_ai_calls": len(h.trace("execute")), "evidence_calls": len(h.trace("evidence")),
                            "provider_peer_eof": [dict(row) for row in peers]}
                    private_json(tmp_path / "case-evidence.json", safe)
            finally:
                h.fault()
                for path in tmp_path.glob("entered-*.json"):
                    (tmp_path / path.name.replace("entered-", "release-").replace(".json", "")).touch()
                try:
                    async with h.provider_sql.begin() as db:
                        await db.execute(text("UPDATE fake_barriers SET released=true"))
                except Exception:
                    pass  # Child cleanup must also run after failed setup/database evidence.
                for name in ("cs", "ai", "provider"):
                    await h.stop(name)
                for handle in h.handles:
                    handle.close()
                for sock in listeners.values():
                    sock.close()
                for engine in engines.values():
                    await engine.dispose()
                if previous_allowed is None:
                    allowed.restore()
                else:
                    allowed.clear(); allowed.update(previous_allowed)
                sensitive = [*tokens.values(), ai_url.password, h.envs["ai"]["ENCRYPTION_KEY"],
                             "SYNTHETIC_PROVIDER_SECRET", "synthetic-model-key"]
                for log_path in tmp_path.glob("*.log"):
                    log_path.chmod(0o600)
                    contents = log_path.read_text(errors="replace")
                    assert not any(value and value in contents for value in sensitive), "Synthetic private input leaked into child log"
