"""Run the existing actual-CRM matrix on disposable native PostgreSQL peers.

No container daemon or inherited application environment is used. Application
behavior and assertions come from business_v2_e2e.run without substitutions.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import traceback
from urllib.parse import quote
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
SAFE_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "LANG"}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env={k: v for k, v in os.environ.items()
                               if not k.upper().startswith("GIT_")},
                          encoding="utf-8", timeout=15).stdout.strip()


async def cleanup_step(errors, resource, action):
    """Attempt each owned resource independently, retaining only safe diagnostics."""
    try:
        result = action()
        if hasattr(result, "__await__"):
            await result
        return True
    except (Exception, asyncio.CancelledError) as error:
        errors.append(dict(resource=resource, error_type=type(error).__name__))
        return False


async def stop_process(process):
    """Join an owned child, escalating from terminate to kill on timeout."""
    if process.poll() is None:
        try:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait, timeout=10)


async def close_resources(client, processes, handles, errors):
    """A failed client or child cannot skip release of another owned resource."""
    await cleanup_step(errors, "http_client", client.aclose)
    for name, process in processes.items():
        await cleanup_step(errors, "process_" + name, lambda p=process: stop_process(p))
    for index, handle in enumerate(handles):
        await cleanup_step(errors, "log_" + str(index), handle.close)


def validate_pg18_binaries(directory):
    """Reject incorrect local tools before initializing any disposable cluster."""
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("initdb", "postgres", "pg_ctl"):
        version = subprocess.run([str(directory / (name + suffix)), "--version"],
            env=base_env(), check=True, capture_output=True, text=True, timeout=10).stdout
        if not re.search(r"\(PostgreSQL\) 18(?:\.|\s|$)", version):
            raise RuntimeError("PostgreSQL18 tools are required")


async def require_server_major(connection, major):
    if int(await connection.fetchval("SHOW server_version_num")) // 10000 != major:
        raise RuntimeError("Unexpected disposable PostgreSQL major version")


def base_env():
    return {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV} | {
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def python_executable(path: Path) -> Path:
    """Keep the venv launcher path: resolving its symlink loses venv packages."""
    return Path(os.path.abspath(path))


def main():
    if not __debug__:
        raise SystemExit("Run the native acceptance runner without python -O")
    if os.name == "nt" and not sys.flags.utf8_mode:
        raise SystemExit("Use python -X utf8 -m tests.business_v2_e2e.native on Windows")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cs-root", "crm-root", "ai-python", "cs-python", "crm-python",
                 "pg16-admin-input", "pg18-bin", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--tzdata-dir", type=Path)
    parser.add_argument("--channel", choices=("TELEGRAM", "WEBSITE_WIDGET"), default="TELEGRAM")
    parser.add_argument("--managed-contract", choices=(2, 3), type=int, default=2)
    args = parser.parse_args()
    roots = {"ai": ROOT, "cs": args.cs_root.resolve(), "crm": args.crm_root.resolve()}
    pythons = {name: python_executable(getattr(args, name + "_python")) for name in roots}
    state = args.output.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    sources = {}
    timezone_env = {}
    timezone_digest = None
    if args.tzdata_dir:
        timezone_root = args.tzdata_dir.resolve()
        timezone_digest = hashlib.sha256((timezone_root / "Europe/Minsk").read_bytes()).hexdigest()
        timezone_env["PYTHONTZPATH"] = str(timezone_root)
    for name, root in roots.items():
        if (Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root
                or (root / ".env").exists() or git(root, "status", "--porcelain")):
            raise SystemExit("Clean source candidates without .env are required")
        sources[name] = {"revision": git(root, "rev-parse", "HEAD"),
            "tree": git(root, "rev-parse", "HEAD^{tree}")}
    admin = json.loads(args.pg16_admin_input.read_text())
    if (admin.get("disposable") != "yes" or admin.get("host") != "127.0.0.1"
            or not 1 <= int(admin["port"]) <= 65535):
        raise SystemExit("Explicit disposable loopback PG16 admin input is required")
    pg18_bin = args.pg18_bin.resolve()
    suffix = uuid4().hex[:12]
    role = "native_e2e_" + suffix
    password = secrets.token_urlsafe(32)
    databases = {name: "native_e2e_" + name + "_" + suffix for name in roots}
    assert re.fullmatch(r"[a-z0-9_]+", role + "".join(databases.values()))
    listeners = []
    ports = {}
    for name in ("ai", "cs", "crm", "model", "pg18"):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        ports[name] = listener.getsockname()[1]
        listeners.append(listener)
    # Install before HTTP/ORM/provider imports. Only these owned loopback peers
    # are admitted; there are no provider keys in the child environment.
    from tests.goal5_business_bridge.network import install_guard, self_check
    install_guard([("127.0.0.1", int(admin["port"])),
                   *(("127.0.0.1", port) for port in ports.values())], allow_bound_loopback=True)
    self_check()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from tests.business_v2_e2e.run import Harness, private
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import asyncpg

    def db_url(name):
        port = ports["pg18"] if name == "crm" else int(admin["port"])
        return ("postgresql+asyncpg://" + role + ":" + quote(password, safe="")
                + "@127.0.0.1:" + str(port) + "/" + databases[name])

    def sync_process(command, cwd, env, log, timeout=90):
        with (state / log).open("xb") as handle:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=timeout)
            except BaseException:
                process.kill()
                process.wait(timeout=10)
                raise
        if code:
            raise RuntimeError("Native child failed; inspect private " + log)

    class NativeHarness(Harness):
        def __init__(self):
            super().__init__(state, {name: db_url(name) for name in roots}, None, None, {})
            self.handles = []
            self.grant.update(channel=args.channel, provider_namespace=(
                "main-chota.telegram.v1" if args.channel == "TELEGRAM" else "main-chota.website-widget.v1"))
            self.threads = {}
            self.website_sessions = {}
            self.message_number = 0
            self.tokens.update(cp_lifecycle=secrets.token_urlsafe(36),
                ai_lifecycle=secrets.token_urlsafe(36))

        def url(self, name):
            return "http://127.0.0.1:" + str(ports[name])

        internal_url = url

        async def setup(self):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()).decode()
            pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            self.config = dict(self.ids, roots={k: str(v) for k, v in roots.items()},
                state=str(state), ports=ports, native_runtime=True, channel=args.channel,
                managed_contract=args.managed_contract, owner_id=self.grant["owner_id"],
                cp_lifecycle_token=self.tokens["cp_lifecycle"],
                allowlist=[("127.0.0.1", int(admin["port"])),
                    *(("127.0.0.1", port) for port in ports.values())])
            private(state / "config.json", self.config)
            grant = dict(owner_id=self.grant["owner_id"], integration_id=self.ids["integration"],
                channel=args.channel, agent_id=self.ids["agent"], agent_owner_id=self.ids["owner"])
            ai_env = dict(POSTGRES_HOST="127.0.0.1", POSTGRES_PORT=str(admin["port"]),
                POSTGRES_DB=databases["ai"], POSTGRES_USER=role, POSTGRES_PASSWORD=password,
                POSTGRES_ECHO="false", ENCRYPTION_KEY=Fernet.generate_key().decode(),
                LLM_BASE_URL=self.url("model") + "/v1", KNOWLEDGE_BASE_ENABLED="false",
                PRIVACY_FILTER_ENABLED="false", SUBSCRIPTION_CHECK_ENABLED="false",
                BUSINESS_CAPABILITIES_ENABLED="true", BUSINESS_CRM_URL=self.url("crm"),
                BUSINESS_CRM_PRIVATE_KEY=pem, BUSINESS_CRM_KEY_ID="synthetic",
                BUSINESS_CONFIG_ORIGINS='["https://chota.by"]',
                BUSINESS_CONVERSATION_URL=self.url("cs"), BUSINESS_CONVERSATION_TOKEN=self.tokens["effect"],
                MANAGED_AI_AUTH_PRINCIPALS=json.dumps([dict(principal_id="synthetic-cs",
                    token=self.tokens["ai"], caller="conversation-service",
                    execution_namespace="main-chota.conversation.v1", grants=[grant])]))
            cs_env = dict(DATABASE_URL=db_url("cs"), LOG_LEVEL="ERROR",
                CONVERSATION_AUTH_PRINCIPALS=json.dumps([
                    dict(principal_id="synthetic-worker", token=self.tokens["cs"], grants=[self.grant]),
                    dict(principal_id="synthetic-business-effect", role="BUSINESS_EFFECT",
                        token=self.tokens["effect"], grants=[dict(self.grant,
                            provider_namespace="main-chota.conversation.v1")])]),
                MANAGED_AI_BASE_URL=self.url("ai"), MANAGED_AI_BINDINGS=json.dumps([
                    dict(owner_id=self.grant["owner_id"], integration_id=self.ids["integration"],
                        channel=args.channel, agent_id=self.ids["agent"], service_token=self.tokens["ai"])]))
            if args.channel == "TELEGRAM":
                cs_env.update(TELEGRAM_CANARY_INTEGRATION_IDS=json.dumps([self.ids["integration"]]),
                    TELEGRAM_WORKER_ENABLED="true", TELEGRAM_WORKER_PRINCIPAL_ID="synthetic-worker")
            else:
                cs_env.update(WEBSITE_BINDINGS=json.dumps([dict(owner_id=self.grant["owner_id"],
                    integration_id=self.ids["integration"], agent_id=self.ids["agent"],
                    **({"capability_generation": 1} if args.managed_contract == 3 else {}))]),
                    WEBSITE_WORKER_ENABLED="true", WEBSITE_WORKER_PRINCIPAL_ID="synthetic-worker")
                if args.managed_contract == 3:
                    cs_env.update(WEBSITE_SESSION_CAPABILITIES_ENABLED="true",
                        WEBSITE_HANDOFF_INTEGRATION_IDS=json.dumps([self.ids["integration"]]),
                        TURN_SETTLING_POLICIES=json.dumps([dict(channel="WEBSITE_WIDGET",
                            integration_id=self.ids["integration"], version="native-business-v3",
                            quiet_window_ms=100, max_wait_ms=1000)]))
            if args.managed_contract == 3:
                ai_env.update(HANDOFF_AI_ENROLLED_SCOPES=json.dumps([{
                    key: grant[key] for key in ("owner_id", "integration_id", "channel", "agent_id")}]),
                    HANDOFF_CONVERSATION_URL=self.url("cs"),
                    HANDOFF_LIFECYCLE_TOKEN=self.tokens["ai_lifecycle"])
                if args.channel == "TELEGRAM":
                    cs_env["TELEGRAM_HANDOFF_INTEGRATION_IDS"] = json.dumps([self.ids["integration"]])
                cs_env.update(USER_SERVICE_BASE_URL=self.url("model"),
                    HANDOFF_CP_BASE_URL=self.url("model"),
                    HANDOFF_AI_BASE_URL=self.url("ai"), INBOX_LIFECYCLE_TOKEN=self.tokens["cp_lifecycle"],
                    HANDOFF_LIFECYCLE_TOKEN=self.tokens["ai_lifecycle"])
            crm_env = dict(DATABASE_URL=db_url("crm"), USER_SERVICE_URL=self.url("model"),
                ALLOWED_ORIGINS='["https://chota.by"]', CRM_AGENT_PUBLIC_KEYS_JSON=json.dumps({"synthetic": pub}))
            self.envs = {name: base_env() | timezone_env | {"BUSINESS_E2E_CONFIG": str(state / "config.json")}
                | extra for name, extra in (("ai", ai_env), ("cs", cs_env), ("crm", crm_env), ("model", {}))}
            for name in roots:
                await asyncio.to_thread(sync_process, [str(pythons[name]),
                    str(ROOT / "tests/business_v2_e2e/native_child.py"), "migrate", name],
                    roots[name], self.envs[name], name + "-migrate.log")
            for name in ("model", "crm", "ai", "cs"):
                await self.start(name)
            await self.seed()
            if args.managed_contract == 3:
                provisioned = await self.request("cs", "POST", "/internal/v1/inbox-grants/provision",
                    dict(contract_version=1, command_id=str(uuid4()), owner_id=self.grant["owner_id"],
                        integration_id=self.ids["integration"], channel=args.channel,
                        agent_id=self.ids["agent"], expected_binding_generation=1),
                    {"X-Inbox-Lifecycle-Token": self.tokens["cp_lifecycle"]})
                assert provisioned["active"] and provisioned["agent_lifecycle_generation"] == 1

        async def turn(self, message, steps, thread=None):
            await self.request("model", "POST", "/script", steps)
            headers = {"X-Service-Token": self.tokens["cs"]}
            thread = thread or str(uuid4())
            self.message_number += 1
            identity = dict(owner_id=self.grant["owner_id"], integration_id=self.ids["integration"],
                agent_id=self.ids["agent"])
            if args.channel == "TELEGRAM":
                if thread not in self.threads:
                    self.threads[thread] = 4000000 + len(self.threads)
                chat = self.threads[thread]
                identity.update(provider_thread_id=str(chat), bot_id=123456,
                    provider_event_id=f"b123456c{chat}m{self.message_number}")
                body = identity | dict(text=message, desired_route="MANAGED", update_id=self.message_number,
                    message_id=self.message_number, chat_id=chat, sender_id=chat)
                route, table = "telegram", "telegram_admissions"
            elif args.managed_contract == 3:
                if thread not in self.website_sessions:
                    # Separate synthetic visitors share the integration quota.
                    # Use actual issuance/admission; never seed a retained grant.
                    client_number = len(self.website_sessions) + 1
                    assert client_number <= 254
                    self.website_sessions[thread] = await self.request("cs", "POST",
                        "/internal/v1/website/sessions", identity | dict(binding_generation=1,
                            client_ip=f"192.0.2.{client_number}"), headers, expected=201)
                credential = self.website_sessions[thread]
                body = identity | dict(binding_generation=1, session_id=credential["session_id"],
                    session_token=credential["session_token"], message_id=str(uuid4()), text=message)
                route, table = "website/sessions", "website_admissions"
            else:
                identity.update(session_id=thread, message_id=str(uuid4()))
                body = identity | dict(text=message, desired_route="MANAGED")
                route, table = "website", "website_admissions"
            admission = await self.request("cs", "POST", "/internal/v1/" + route + "/admissions", body, headers)
            assert admission["route"] == "MANAGED" and not admission["replayed"]
            await asyncio.to_thread(sync_process, [str(pythons["cs"]),
                str(ROOT / "tests/business_v2_e2e/native_child.py"), "worker"], roots["cs"],
                self.envs["cs"], "worker-" + str(self.message_number) + ".log", 90)
            found = await self.sql("cs", "SELECT owned.turn_id, turn.managed_contract_version FROM " + table + " admitted "
                "JOIN turn_input_messages owned ON owned.message_id=admitted.canonical_message_id "
                "JOIN agent_turns turn ON turn.id=owned.turn_id "
                "WHERE admitted.id=$1", UUID(admission["admission_id"]))
            assert len(found) == 1
            result = await self.request("cs", "GET", "/internal/v1/turns/" + str(found[0]["turn_id"]),
                headers=headers)
            assert result["status"] in {"COMPLETED", "FAILED"}
            assert found[0]["managed_contract_version"] == args.managed_contract
            assert len(result["operation_refs"]) <= 32
            assert all(set(value) == {"origin", "operation_id", "outcome"}
                and value["origin"] == "AI_AGENT_SERVICE" for value in result["operation_refs"])
            private(state / ("turn-" + result["id"] + ".json"), {"turn": result})
            return result

        async def start(self, name):
            runtime = pythons["ai" if name == "model" else name]
            handle = (state / (name + ".log")).open("xb")
            self.handles.append(handle)
            process = subprocess.Popen([str(runtime), str(ROOT / "tests/business_v2_e2e/native_child.py"), name],
                cwd=roots["ai" if name == "model" else name], env=self.envs[name],
                stdout=handle, stderr=subprocess.STDOUT)
            self.processes[name] = process
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(name + " exited; inspect its private log")
                try:
                    if (await self.client.get(self.url(name) + "/health", timeout=.3)).status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(.06)
            raise RuntimeError(name + " readiness timeout")

        async def recover(self, turn_id):
            await asyncio.to_thread(sync_process, [str(pythons["ai"]),
                str(ROOT / "tests/business_v2_e2e/native_child.py"), "recover", turn_id],
                roots["ai"], self.envs["ai"], "recovery.log", 40)
            return json.loads((state / "recovery.jsonl").read_text().splitlines()[-1])

        async def close(self):
            await close_resources(self.client, self.processes, self.handles, cleanup_errors)

    record = dict(at_utc=datetime.now(timezone.utc).isoformat(), sources=sources,
        status="FAIL", external_provider_requests=0, production_mutations=False,
        live_model=False, real_crm=True, cases=[], owned_database_cleanup=False,
        explicit_timezone_sha256=timezone_digest, pg18_database_dropped=False,
        controller_utf8_mode=bool(sys.flags.utf8_mode), channel=args.channel,
        admission_mode=f"MANAGED_V{args.managed_contract}_REAL_WORKER",
        enrollment_proofs="SYNTHETIC_CP_REAL_AI" if args.managed_contract == 3 else "NOT_USED")
    pg_process = None
    pg_handle = None
    created = []
    role_created = False
    harness = None
    cleanup_errors = []
    crm_created = False
    cleanup_attempted = False
    pwfile = state / "pg18-password.txt"
    pgdata = state / "pg18-data"
    exe = ".exe" if os.name == "nt" else ""

    async def pg16_connect():
        return await asyncpg.connect(host="127.0.0.1", port=int(admin["port"]),
            user=admin["user"], password=admin["password"], database="postgres",
            timeout=5, command_timeout=15)

    async def validate_pg16():
        connection = await pg16_connect()
        try:
            await require_server_major(connection, 16)
        finally:
            await connection.close()

    async def execute():
        nonlocal harness, role_created
        pg16 = await pg16_connect()
        try:
            await require_server_major(pg16, 16)
            await pg16.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
            role_created = True
            for name in ("ai", "cs"):
                await pg16.execute(f"CREATE DATABASE {databases[name]} OWNER {role} ENCODING 'UTF8'")
                created.append(databases[name])
            harness = NativeHarness()
            await harness.setup()
            await harness.cases()
            record["cases"] = harness.results
            record["status"] = "PASS"
        finally:
            await cleanup_step(cleanup_errors, "pg16_execution_connection", pg16.close)

    async def cleanup_databases():
        nonlocal role_created
        if created or role_created:
            pg16 = await pg16_connect()
            try:
                await require_server_major(pg16, 16)
                for database in list(reversed(created)):
                    if await cleanup_step(cleanup_errors, "pg16_" + database,
                            lambda db=database: pg16.execute(f"DROP DATABASE {db} WITH (FORCE)")):
                        created.remove(database)
                if role_created:
                    role_created = not await cleanup_step(cleanup_errors, "pg16_role",
                        lambda: pg16.execute(f"DROP ROLE {role}"))
            finally:
                await cleanup_step(cleanup_errors, "pg16_cleanup_connection", pg16.close)

    async def cleanup_crm_database():
        if crm_created:
            connection = await asyncpg.connect(host="127.0.0.1", port=ports["pg18"],
                user=role, password=password, database="postgres", timeout=5, command_timeout=15)
            try:
                await require_server_major(connection, 18)
                await connection.execute(f"DROP DATABASE {databases['crm']} WITH (FORCE)")
                record["pg18_database_dropped"] = True
            finally:
                await cleanup_step(cleanup_errors, "pg18_cleanup_connection", connection.close)

    async def cleanup():
        await cleanup_step(cleanup_errors, "password_file", lambda: pwfile.unlink(missing_ok=True))
        if harness:
            record["cases"] = harness.results
            await cleanup_step(cleanup_errors, "harness", harness.close)
        await cleanup_step(cleanup_errors, "pg16_resources", cleanup_databases)
        await cleanup_step(cleanup_errors, "pg18_database", cleanup_crm_database)
        record["owned_database_cleanup"] = (
            not created and not role_created and (not crm_created or record["pg18_database_dropped"]))
        for index, listener in enumerate(listeners):
            await cleanup_step(cleanup_errors, "listener_" + str(index), listener.close)
        if pg_process and pg_process.poll() is None:
            def stop_pg18():
                subprocess.run([str(pg18_bin / ("pg_ctl" + exe)), "-D", str(pgdata),
                    "stop", "-m", "fast", "-w", "-t", "15"], env=base_env(), check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                pg_process.wait(timeout=10)
            if not await cleanup_step(cleanup_errors, "pg18_stop", stop_pg18):
                await cleanup_step(cleanup_errors, "pg18_stop_fallback", lambda: stop_process(pg_process))
        if pg_handle:
            await cleanup_step(cleanup_errors, "pg18_log", pg_handle.close)
        record["pg18_stopped"] = pg_process is None or pg_process.poll() is not None

    try:
        validate_pg18_binaries(pg18_bin)
        asyncio.run(validate_pg16())
        pwfile.write_text(password)
        pwfile.chmod(0o600)
        sync_process([str(pg18_bin / ("initdb" + exe)), "-D", str(pgdata), "-U", role,
            "--pwfile=" + str(pwfile), "--auth=password", "--encoding=UTF8", "--locale=C"],
            state, base_env(), "pg18-init.log")
        pwfile.unlink()
        for listener in listeners:
            listener.close()
        pg_handle = (state / "pg18.log").open("xb")
        pg_process = subprocess.Popen([str(pg18_bin / ("postgres" + exe)), "-D", str(pgdata),
            "-h", "127.0.0.1", "-p", str(ports["pg18"])], env=base_env(),
            stdout=pg_handle, stderr=subprocess.STDOUT)

        async def prepare_crm():
            nonlocal crm_created
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    connection = await asyncpg.connect(host="127.0.0.1", port=ports["pg18"],
                        user=role, password=password, database="postgres", timeout=1)
                    try:
                        await require_server_major(connection, 18)
                        await connection.execute(f"CREATE DATABASE {databases['crm']} OWNER {role} ENCODING 'UTF8'")
                        crm_created = True
                    finally:
                        await connection.close()
                    return
                except (OSError, asyncpg.PostgresError):
                    await asyncio.sleep(.1)
            raise RuntimeError("Disposable PostgreSQL18 readiness failed")
        async def run_native():
            nonlocal cleanup_attempted
            try:
                await prepare_crm()
                await execute()
            finally:
                cleanup_attempted = True
                await cleanup()

        asyncio.run(run_native())
    except Exception as error:
        record["error_type"] = type(error).__name__
        record["error_frames"] = [dict(file=Path(frame.filename).name,
            line=frame.lineno, function=frame.name) for frame in traceback.extract_tb(error.__traceback__)[-6:]]
        # Assertion labels are code-owned scenario names; never expose repr or
        # HTTP payloads, DSNs, credentials or arbitrary exception text.
        if isinstance(error, AssertionError) and re.fullmatch(r"[A-Za-z0-9_]+", str(error)):
            record["failing_case"] = str(error)
        elif isinstance(error, AssertionError):
            failed_request = re.match(r"^(ai|cs|crm|model) (/[^ ]+): ([0-9]{3}) ", str(error))
            if failed_request:
                record["failed_request"] = dict(service=failed_request[1], status=int(failed_request[3]),
                    route=re.sub(r"[0-9a-f-]{36}", "{id}", failed_request[2]))
    finally:
        if not cleanup_attempted:
            try:
                asyncio.run(cleanup())
            except Exception as error:
                cleanup_errors.append(dict(resource="outer_cleanup", error_type=type(error).__name__))
        record["source_unchanged"] = False
        try:
            record["source_unchanged"] = all(git(root, "rev-parse", "HEAD") == sources[name]["revision"]
                and not git(root, "status", "--porcelain") for name, root in roots.items())
            record["runtime_manifest_sha256"] = (hashlib.sha256((state / "runtime-manifests.jsonl").read_bytes()).hexdigest()
                if (state / "runtime-manifests.jsonl").exists() else None)
        except Exception as error:
            cleanup_errors.append(dict(resource="source_evidence", error_type=type(error).__name__))
        record["cleanup_errors"] = cleanup_errors
        if not all(record[key] for key in ("owned_database_cleanup", "pg18_database_dropped",
                "pg18_stopped", "source_unchanged")) or cleanup_errors:
            record["status"] = "FAIL"
        (state / "summary.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
