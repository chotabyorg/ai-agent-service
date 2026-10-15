"""Actual Conversation/AI/provider process harness over three native PG16 DBs."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url

from tests.goal5_business_bridge.proxy import ForwardProxy

_CS_ROOT = os.environ.get("GOAL5_CS_ROOT")
CS_ROOT = Path(_CS_ROOT).resolve() if _CS_ROOT else None
AI_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/internal/v1"


def private_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False))
    path.chmod(0o600)


def clean_env(python: Path, pg_bin: Path) -> dict[str, str]:
    return {
        "PATH": f"{python.parent}:{pg_bin}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }


def listen():
    value = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    value.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    value.bind(("127.0.0.1", 0))
    value.listen(64)
    return value


@dataclass
class PairedHarness:
    root: Path
    config: dict = field(repr=False)
    urls: dict = field(repr=False)
    engines: dict = field(repr=False)
    listeners: dict = field(repr=False)
    client: httpx.AsyncClient = field(repr=False)
    tokens: dict = field(repr=False)
    ids: dict = field(repr=False)
    grant: dict = field(repr=False)
    provider_proxy: ForwardProxy = field(repr=False)
    effect_proxy: ForwardProxy = field(repr=False)
    processes: dict = field(default_factory=dict, repr=False)
    handles: dict = field(default_factory=dict, repr=False)
    envs: dict = field(default_factory=dict, repr=False)
    bootstrap: dict = field(default_factory=dict, repr=False)

    def url(self, name):
        return f"http://127.0.0.1:{self.listeners[name].getsockname()[1]}"

    def read(self, name):
        return json.loads((self.root / name).read_text())

    async def ready(self, name):
        deadline = asyncio.get_running_loop().time() + 25
        while asyncio.get_running_loop().time() < deadline:
            process = self.processes[name]
            if process.poll() is not None:
                raise AssertionError(f"Owned {name} process exited; inspect private test log")
            try:
                response = await self.client.get(self.url(name) + "/health", timeout=0.4)
                if response.status_code == 200:
                    if name == "cs":
                        response = await self.client.get(self.url(name) + "/ready", timeout=0.5)
                        if response.status_code != 200:
                            await asyncio.sleep(0.04)
                            continue
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.04)
        raise AssertionError(f"Owned {name} process readiness deadline exceeded")

    async def start(self, name):
        if name in self.processes and self.processes[name].poll() is None:
            return
        module = {
            "provider": "tests.goal5_business_bridge.provider_bootstrap",
            "ai": "tests.goal5_business_bridge.ai_bootstrap",
            "cs": "tests.managed_cross_service.runtime_bootstrap",
        }[name]
        root = CS_ROOT if name == "cs" else AI_ROOT
        if root is None:
            raise RuntimeError("Explicit Goal5 Conversation candidate path required")
        python = Path(self.config["cs_python"] if name == "cs" else self.config["ai_python"])
        log_path = self.root / f"{name}-{len(self.handles)}.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        handle = os.fdopen(descriptor, "wb")
        self.handles[name] = handle
        self.processes[name] = subprocess.Popen(
            [str(python), "-m", module], cwd=root, env=self.envs[name],
            stdout=handle, stderr=handle, pass_fds=(self.listeners[name].fileno(),),
            start_new_session=True,
        )
        await self.ready(name)

    async def stop(self, name, *, abrupt=False):
        process = self.processes.get(name)
        if process is None or process.poll() is not None:
            return
        process.kill() if abrupt else process.terminate()
        try:
            await asyncio.to_thread(process.wait, 8)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 3)

    async def restart(self, name, *, abrupt=False):
        await self.stop(name, abrupt=abrupt)
        await self.start(name)

    async def request(self, method, path, value=None, principal="owner"):
        return await self.client.request(
            method, self.url("cs") + PREFIX + path, json=value,
            headers={"X-Service-Token": self.tokens[principal]},
        )

    async def ingest(self, text_value="Запиши меня на 12:00", *, thread=None):
        value = dict(self.grant, provider_thread_id=thread or str(uuid4()),
                     provider_event_id=str(uuid4()), text=text_value)
        response = await self.request("POST", "/inbound-events", value)
        assert response.status_code == 201, response.text[:200]
        return response.json()

    async def telegram_ingest(self, text_value, *, thread=None, trace=None):
        chat_id = thread or str(secrets.randbelow(899_999_999) + 100_000_000)
        bot_id = 731_005
        message_id = secrets.randbelow(900_000_000) + 1
        value = {
            "owner_id": self.grant["owner_id"],
            "integration_id": self.ids["integration"],
            "agent_id": self.ids["agent"],
            "provider_thread_id": chat_id,
            "provider_event_id": f"b{bot_id}c{chat_id}m{message_id}",
            "update_id": secrets.randbelow(900_000_000) + 1,
            "chat_id": int(chat_id), "sender_id": int(chat_id),
            "message_id": message_id, "bot_id": bot_id,
            "text": text_value, "desired_route": "MANAGED",
        }
        if trace is not None:
            trace["ingress_request_started_monotonic_ns"] = time.monotonic_ns()
        response = await self.request("POST", "/telegram/admissions", value)
        if trace is not None:
            trace["ingress_accepted_monotonic_ns"] = time.monotonic_ns()
        assert response.status_code == 200, response.text[:200]
        async with self.engines["conversation"].connect() as connection:
            conversation_id = await connection.scalar(text(
                "SELECT id FROM conversations WHERE owner_id=:owner_id "
                "AND integration_id=:integration_id AND channel='TELEGRAM' "
                "AND provider_thread_id=:thread"
            ), {"owner_id": self.grant["owner_id"],
                "integration_id": self.ids["integration"], "thread": chat_id})
        assert conversation_id is not None
        conversation = await self.request("GET", f"/conversations/{conversation_id}")
        assert conversation.status_code == 200, conversation.text[:200]
        return {"admission": response.json(), "conversation": conversation.json()}

    async def turn(self, accepted, first=None, last=None):
        conversation = (await self.request(
            "GET", "/conversations/" + accepted["conversation"]["id"]
        )).json()
        value = {
            "selection_key": str(uuid4()),
            "input_first_sequence": first or 1,
            "input_last_sequence": last or conversation["last_message_sequence"],
            "expected_input_revision": conversation["input_revision"],
            "expected_authority_revision": conversation["authority_revision"],
        }
        response = await self.request(
            "POST", "/conversations/" + conversation["id"] + "/turns", value
        )
        assert response.status_code == 201, response.text[:200]
        return response.json()["turn"]

    async def claim(self, turn):
        response = await self.request(
            "POST", f"/turns/{turn['id']}/claim",
            {"worker_ref": "goal5-business-bridge", "lease_seconds": 300},
        )
        assert response.status_code == 200, response.text[:200]
        return response.json()

    async def execute(self, claim):
        return await self.request(
            "POST", f"/turns/{claim['turn']['id']}/execute",
            {"claim_epoch": claim["turn"]["claim_epoch"], "claim_token": claim["claim_token"]},
        )

    async def script(self, *, slots=("12:00",), execute=None, release=(), clear=True,
                     model=None):
        from tests.goal3_business_cross_service.fake_provider import completion, tool_call

        if model is None:
            actions = [{"capability": "booking.create", "arguments": {"slot": slot}}
                       for slot in slots]
            model = [
                {"body": completion(None, [tool_call(
                    "business_execute_plan", {"actions": actions})])},
                {"body": completion("Запись выполнена")},
            ]
        settings = {}
        if execute is not None:
            settings["execute"] = execute
        response = await self.client.post(
            self.url("provider") + "/__test__/script",
            json={"clear_model": clear, "model": model, "settings": settings, "release": list(release)},
            timeout=3,
        )
        assert response.status_code == 200, response.text[:200]

    async def configure_settling(self, policy):
        """Restart the owned Conversation process with an explicit test-only policy."""
        if policy is None:
            self.envs["cs"].pop("TURN_SETTLING_POLICIES", None)
        else:
            self.envs["cs"]["TURN_SETTLING_POLICIES"] = json.dumps([policy])
        await self.restart("cs")

    async def wait_burst_ready(self, burst_id, timeout=10):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with self.engines["conversation"].connect() as connection:
                ready = await connection.scalar(text(
                    "SELECT ready_at <= clock_timestamp() FROM settling_bursts WHERE id=:id"
                ), {"id": burst_id})
            if ready:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("Owned Conversation settling burst did not become ready")

    async def insert_interleaved_history(self, conversation_id, origin_turn_id, customer_seq):
        """Record one settling split with canonical interleaved history.

        Customer messages arrive through real Conversation HTTP ingress. The
        owned test database then records the frozen burst metadata and failed
        chunk boundary so the later confirmation exercises the real sidecar
        query over a split, interleaved history.
        """
        from hashlib import sha256

        agent_message_id = uuid4()
        candidate_turn_id = uuid4()
        token = sha256(f"goal5-bridge-{uuid4()}".encode()).hexdigest()
        terminal = sha256(f"goal5-terminal-{uuid4()}".encode()).hexdigest()
        creation = sha256(f"goal5-selection-{uuid4()}".encode()).hexdigest()
        async with self.engines["conversation"].begin() as connection:
            row = (await connection.execute(text(
                "SELECT c.last_message_sequence,c.input_revision,c.authority_revision,"
                "m.input_revision AS message_revision,m.settling_revision,"
                "m.settling_burst_id,m.sequence FROM conversations c JOIN messages m "
                "ON m.conversation_id=c.id AND m.sequence=:sequence "
                "WHERE c.id=:conversation_id"
            ), {"sequence": customer_seq, "conversation_id": conversation_id})).mappings().one()
            burst_id = row["settling_burst_id"]
            assert burst_id is not None
            next_sequence = int(row["last_message_sequence"]) + 1
            await connection.execute(text(
                "UPDATE conversations SET last_message_sequence=:sequence "
                "WHERE id=:conversation_id"
            ), {"sequence": next_sequence, "conversation_id": conversation_id})
            await connection.execute(text(
                "INSERT INTO messages(id,conversation_id,sequence,actor,direction,"
                "content_type,text,source_agent_turn_id) VALUES(:id,:conversation_id,:sequence,"
                "'AGENT','OUTBOUND','TEXT','Поясните, пожалуйста, время.',:origin_turn_id)"
            ), {"id": agent_message_id, "conversation_id": conversation_id,
                "sequence": next_sequence, "origin_turn_id": origin_turn_id})

            now = await connection.scalar(text("SELECT clock_timestamp()"))
            await connection.execute(text(
                "INSERT INTO agent_turns(id,conversation_id,selection_key,creation_fingerprint,"
                "input_first_sequence,input_last_sequence,context_last_sequence,input_revision,"
                "authority_revision,settling_revision,settling_burst_id,status,claim_epoch,"
                "claim_token_digest,worker_ref,lease_expires_at,started_at,completed_at,"
                "failure_code,terminal_fingerprint,operation_refs,created_at) VALUES("
                ":id,:conversation_id,:selection_key,:creation_fingerprint,:sequence,:sequence,"
                ":sequence,:input_revision,:authority_revision,:settling_revision,:burst_id,"
                "'FAILED',1,:claim_digest,'goal5-history-fixture',:lease_expires,:now,:now,"
                "'PROCESSING_ERROR',:terminal_fingerprint,'[]'::jsonb,:now)"
            ), {"id": candidate_turn_id, "conversation_id": conversation_id,
                "selection_key": uuid4(), "creation_fingerprint": creation,
                "sequence": customer_seq, "input_revision": row["message_revision"],
                "authority_revision": row["authority_revision"],
                "settling_revision": row["settling_revision"],
                "burst_id": burst_id, "claim_digest": token,
                "lease_expires": now, "now": now,
                "terminal_fingerprint": terminal})
            message_id = await connection.scalar(text(
                "SELECT id FROM messages WHERE conversation_id=:conversation_id "
                "AND sequence=:sequence"
            ), {"conversation_id": conversation_id, "sequence": customer_seq})
            await connection.execute(text(
                "INSERT INTO turn_input_messages(message_id,conversation_id,turn_id,sequence,ordinal)"
                " VALUES(:message_id,:conversation_id,:turn_id,:sequence,0)"
            ), {"message_id": message_id, "conversation_id": conversation_id,
                "turn_id": candidate_turn_id, "sequence": customer_seq})
        return {"agent_message_id": str(agent_message_id),
                "candidate_turn_id": str(candidate_turn_id),
                "burst_id": str(burst_id),
                "agent_sequence": next_sequence}


    async def authority(self, conversation_id, mode="HUMAN"):
        current = (await self.request("GET", "/conversations/" + conversation_id)).json()
        response = await self.request(
            "POST", "/conversations/" + conversation_id + "/control",
            {"expected_authority_revision": current["authority_revision"], "control_mode": mode},
        )
        assert response.status_code == 200, response.text[:200]
        return response.json()

    async def engine(self, kind, query, values=None):
        async with self.engines[kind].connect() as connection:
            return await connection.execute(text(query), values or {})

    async def provider_counts(self):
        async with self.engines["provider"].connect() as connection:
            counts = (await connection.execute(text(
                "SELECT kind,count(*) FROM fake_requests GROUP BY kind"
            ))).all()
            objects = await connection.scalar(text("SELECT count(*) FROM fake_objects"))
        return dict(counts), int(objects or 0)

    async def wait_provider_barrier(self, label, timeout=20):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with self.engines["provider"].connect() as connection:
                state = (await connection.execute(text(
                    "SELECT entered,released FROM fake_barriers WHERE label=:label"
                ), {"label": label})).first()
            if state and state.entered:
                return state
            await asyncio.sleep(0.02)
        raise AssertionError("Owned provider barrier did not reach its durable entered state")

    async def wait_dispatch_expiry(self, origin_turn_id, timeout=20):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with self.engines["ai"].connect() as connection:
                expired = await connection.scalar(text(
                    "SELECT dispatch_lease_expires_at <= clock_timestamp() "
                    "FROM business_operations o JOIN business_plans p ON p.id=o.plan_id "
                    "WHERE p.origin_turn_id=:turn_id AND o.status='DISPATCHING' LIMIT 1"
                ), {"turn_id": origin_turn_id})
            if expired:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("Owned dispatch lease did not reach its database expiry")

    async def release_provider_barrier(self, label):
        response = await self.client.post(
            self.url("provider") + "/__test__/script",
            json={"clear_model": False, "model": [],
                  "settings": {}, "release": [label]}, timeout=3,
        )
        assert response.status_code == 200, response.text[:200]

    async def operations(self, turn_id):
        async with self.engines["ai"].connect() as connection:
            rows = (await connection.execute(text(
                "SELECT o.id,o.status,o.effect_permit_id,o.dispatch_started_at "
                "FROM business_operations o JOIN business_plans p ON p.id=o.plan_id "
                "WHERE p.origin_turn_id=:turn_id ORDER BY o.ordinal"
            ), {"turn_id": turn_id})).mappings().all()
        return [dict(row) for row in rows]

    async def permit_rows(self):
        async with self.engines["conversation"].connect() as connection:
            rows = (await connection.execute(text(
                "SELECT id,turn_id,origin_turn_id,operation_id FROM external_effect_permits "
                "ORDER BY issued_at"
            ))).mappings().all()
        return [dict(row) for row in rows]

    def ai_request(self):
        values = [value for value in self.provider_proxy.snapshot()
                  if value["path"] == "/internal/v1/managed-turns/execute"]
        return json.loads(values[-1]["body"]) if values else None

    async def direct_ai_replay(self, request):
        return await self.client.post(
            self.url("ai") + "/internal/v1/managed-turns/execute", json=request,
            headers={"X-Service-Token": self.tokens["managed"]}, timeout=50,
        )

    def safe_logs(self):
        for handle in self.handles.values():
            handle.flush()
        logs = "\n".join(path.read_text(errors="replace") for path in self.root.glob("*.log"))
        secrets_to_check = [*self.tokens.values(),
                            make_url(self.urls["ai"]).password,
                            make_url(self.urls["conversation"]).password,
                            make_url(self.urls["provider"]).password,
                            self.envs["ai"]["ENCRYPTION_KEY"], "synthetic-model-key"]
        assert all(secret not in logs for secret in secrets_to_check)
        assert "Goal5 network destination forbidden" not in logs


async def prepare_harness(config, inputs, tmp_path, client, engines, listeners):
    from tests.goal5_business_bridge import network

    network.PREIMPORT_GUARD.update(listener.getsockname() for listener in listeners.values())
    tokens = {key: secrets.token_urlsafe(32)
              for key in ("owner", "effect", "managed", "chat", "internal")}
    ids = {key: str(uuid4()) for key in ("agent", "agent_owner", "integration", "connection")}
    grant = {"owner_id": "opaque-goal5-bridge", "integration_id": ids["integration"],
             "channel": "TELEGRAM", "provider_namespace": "main-chota.telegram.v1"}
    private_json(tmp_path / "observations.json", {})
    private_json(tmp_path / "scenario.json", {})
    private_json(tmp_path / "redis.json", {})

    h = PairedHarness(
        tmp_path, config, {key: value["url"] for key, value in config["databases"].items()},
        engines, listeners, client, tokens, ids, grant,
        ForwardProxy(listeners["ai_proxy"], "", "ai"),
        ForwardProxy(listeners["effect_proxy"], "", "effects"),
    )
    h.provider_proxy.upstream = h.url("ai")
    h.effect_proxy.upstream = h.url("cs")
    h.provider_proxy.start()
    h.effect_proxy.start()

    urls = {key: make_url(value) for key, value in h.urls.items()}
    for kind, engine in engines.items():
        async with engine.begin() as connection:
            assert 160000 <= int(await connection.scalar(text("SHOW server_version_num"))) < 170000
            encoding = await connection.scalar(text("SHOW server_encoding"))
            assert encoding == "UTF8", f"{kind} disposable test DB must use UTF8"
            if kind == "provider":
                continue
            tables = (await connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename!='alembic_version' ORDER BY tablename"
            ))).scalars().all()
            if tables:
                identifiers = ",".join('"' + name.replace('"', '""') + '"' for name in tables)
                await connection.execute(text("TRUNCATE " + identifiers + " CASCADE"))

    # Provider schema is created by its own process and then reset between cases.
    provider_cfg = {
        "fd": listeners["provider"].fileno(),
        "port": listeners["provider"].getsockname()[1],
        "database": h.urls["provider"].replace("postgresql+asyncpg://", "postgresql://"),
        "profile": {"mode": "LOOKUP", "native_policy": "DURABLE_TEST",
                    "timeout": 4, "bounded_acceptance": "PROVIDER_ACCEPT_BEFORE_V1"},
    }
    private_json(tmp_path / "provider.json", provider_cfg)
    pg_bin = Path(config["pg_bin"]).resolve()
    provider_env = clean_env(Path(config["ai_python"]), pg_bin)
    provider_env["GOAL3_PROVIDER_CONFIG"] = str(tmp_path / "provider.json")
    h.envs["provider"] = provider_env

    cs_port = listeners["cs"].getsockname()[1]
    ai_port = listeners["ai"].getsockname()[1]
    proxy_ai_port = listeners["ai_proxy"].getsockname()[1]
    effect_proxy_port = listeners["effect_proxy"].getsockname()[1]
    cs_cfg = {"fd": listeners["cs"].fileno(), "port": cs_port,
              "state_dir": str(tmp_path),
              "allowlist": [["127.0.0.1", urls["conversation"].port],
                            ["127.0.0.1", proxy_ai_port]]}
    private_json(tmp_path / "cs-config.json", cs_cfg)
    principals = [
        {"principal_id": "fixture-owner", "token": tokens["owner"], "grants": [grant]},
        {"principal_id": "fixture-business-effect", "role": "BUSINESS_EFFECT",
         "token": tokens["effect"], "grants": [grant]},
    ]
    managed_grant = {"owner_id": grant["owner_id"], "integration_id": ids["integration"],
                     "channel": grant["channel"], "agent_id": ids["agent"],
                     "agent_owner_id": ids["agent_owner"]}
    conversation_binding = {key: managed_grant[key]
                           for key in ("owner_id", "integration_id", "channel", "agent_id")}
    bindings = [{**conversation_binding, "service_token": tokens["managed"]}]
    h.envs["cs"] = clean_env(Path(config["cs_python"]), pg_bin)
    h.envs["cs"].update(
        DATABASE_URL=h.urls["conversation"], LOG_LEVEL="WARNING",
        GOAL2_BOOTSTRAP_CONFIG=str(tmp_path / "cs-config.json"),
        CONVERSATION_AUTH_PRINCIPALS=json.dumps(principals),
        MANAGED_AI_BASE_URL=h.url("ai_proxy"),
        MANAGED_AI_TOTAL_SECONDS="60", MANAGED_AI_READ_SECONDS="60",
        TELEGRAM_CANARY_INTEGRATION_IDS=json.dumps([ids["integration"]]),
        MANAGED_AI_BINDINGS=json.dumps(bindings),
    )

    ai_port_db = urls["ai"].port
    provider_port = listeners["provider"].getsockname()[1]
    ai_cfg = {
        "fd": listeners["ai"].fileno(), "port": ai_port, "state_dir": str(tmp_path),
        "ai_root": str(AI_ROOT), "provider_url": h.url("provider"),
        "profile": {"mode": "LOOKUP", "native_policy": "DURABLE_TEST",
                    "timeout": 4, "proposal_ttl": 300,
                    "confirmation_mode": "SEMANTIC_V1",
                    "bounded_acceptance": "PROVIDER_ACCEPT_BEFORE_V1"},
        "allowlist": [["127.0.0.1", ai_port_db], ["127.0.0.1", provider_port],
                      ["127.0.0.1", effect_proxy_port]],
        "grants": [managed_grant],
        "agents": [{"id": ids["agent"], "owner": ids["agent_owner"],
                    "connection_id": ids["connection"]}],
    }
    private_json(tmp_path / "ai-config.json", ai_cfg)
    h.envs["ai"] = clean_env(Path(config["ai_python"]), pg_bin)
    h.envs["ai"].update(
        POSTGRES_HOST="127.0.0.1", POSTGRES_PORT=str(ai_port_db),
        POSTGRES_DB=urls["ai"].database, POSTGRES_USER=urls["ai"].username,
        POSTGRES_PASSWORD=urls["ai"].password, POSTGRES_ECHO="false",
        ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        GOAL5_AI_CONFIG=str(tmp_path / "ai-config.json"),
        MANAGED_AI_AUTH_PRINCIPALS=json.dumps([{
            "principal_id": "goal5-conversation", "token": tokens["managed"],
            "caller": "conversation-service", "execution_namespace": "main-chota.conversation.v1",
            "grants": [managed_grant],
        }]),
        MANAGED_AI_EXECUTION_SECONDS="50", CHAT_S2S_TOKEN=tokens["chat"],
        CHAT_S2S_AUTH_REQUIRED="true", INTERNAL_API_TOKEN=tokens["internal"],
        REDIS_HOST="", DEBUG="false", LLM_BASE_URL=h.url("provider") + "/v1",
        KNOWLEDGE_BASE_ENABLED="false", BUSINESS_CONVERSATION_URL=h.url("effect_proxy"),
        BUSINESS_CONVERSATION_TOKEN=tokens["effect"],
        BUSINESS_CONVERSATION_TIMEOUT_SECONDS="5",
    )
    h.bootstrap = {"cs": cs_cfg, "ai": ai_cfg, "provider": provider_cfg}
    try:
        await h.start("provider")
        async with engines["provider"].begin() as connection:
            await connection.execute(text(
                "TRUNCATE fake_requests,fake_receipts,fake_objects,fake_keys,"
                "fake_key_authorities,fake_settings,fake_model_queue,fake_barriers CASCADE"
            ))
            await connection.execute(text(
                "UPDATE fake_facts SET values='{}'::jsonb,revisions=CAST(:revisions AS jsonb) WHERE id=1"
            ), {"revisions": '{"catalog":1}'})
        await h.start("ai")
        await h.start("cs")
        return h
    except BaseException:
        await close_harness(h)
        raise


async def close_harness(h):
    for name in ("cs", "ai", "provider"):
        await h.stop(name)
    for proxy in (h.provider_proxy, h.effect_proxy):
        if proxy.thread.is_alive():
            proxy.close()
    for handle in h.handles.values():
        handle.close()
    for engine in h.engines.values():
        await engine.dispose()
