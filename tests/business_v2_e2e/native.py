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
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SAFE_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "LANG"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def base_env():
    return {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV} | {
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def main():
    if os.name == "nt" and not sys.flags.utf8_mode:
        raise SystemExit("Use python -X utf8 -m tests.business_v2_e2e.native on Windows")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cs-root", "crm-root", "ai-python", "cs-python", "crm-python",
                 "pg16-admin-input", "pg18-bin", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--tzdata-dir", type=Path)
    args = parser.parse_args()
    roots = {"ai": ROOT, "cs": args.cs_root.resolve(), "crm": args.crm_root.resolve()}
    pythons = {name: getattr(args, name + "_python").resolve() for name in roots}
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
        if (root / ".env").exists() or git(root, "status", "--porcelain"):
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
                state=str(state), ports=ports, native_runtime=True,
                allowlist=[("127.0.0.1", int(admin["port"])),
                    *(("127.0.0.1", port) for port in ports.values())])
            private(state / "config.json", self.config)
            grant = dict(owner_id=self.grant["owner_id"], integration_id=self.ids["integration"],
                channel="TELEGRAM", agent_id=self.ids["agent"], agent_owner_id=self.ids["owner"])
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
                        channel="TELEGRAM", agent_id=self.ids["agent"], service_token=self.tokens["ai"])]))
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
            await self.client.aclose()
            for process in self.processes.values():
                if process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=10)
            for handle in self.handles:
                handle.close()

    record = dict(at_utc=datetime.now(timezone.utc).isoformat(), sources=sources,
        status="FAIL", external_provider_requests=0, production_mutations=False,
        live_model=False, real_crm=True, cases=[], owned_database_cleanup=False,
        explicit_timezone_sha256=timezone_digest, pg18_database_dropped=False,
        controller_utf8_mode=bool(sys.flags.utf8_mode))
    pg_process = None
    pg_handle = None
    created = []
    role_created = False
    harness = None

    async def execute():
        nonlocal harness, role_created
        pg16 = await asyncpg.connect(host="127.0.0.1", port=int(admin["port"]),
            user=admin["user"], password=admin["password"], database="postgres", timeout=5)
        try:
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
            if harness:
                record["cases"] = harness.results
                await harness.close()
            for database in reversed(created):
                await pg16.execute(f"DROP DATABASE {database} WITH (FORCE)")
            if role_created:
                await pg16.execute(f"DROP ROLE {role}")
            await pg16.close()
            pg18 = await asyncpg.connect(host="127.0.0.1", port=ports["pg18"],
                user=role, password=password, database="postgres", timeout=5)
            try:
                await pg18.execute(f"DROP DATABASE {databases['crm']} WITH (FORCE)")
                record["pg18_database_dropped"] = True
            finally:
                await pg18.close()
            record["owned_database_cleanup"] = True

    try:
        pwfile = state / "pg18-password.txt"
        pwfile.write_text(password)
        pwfile.chmod(0o600)
        pgdata = state / "pg18-data"
        exe = ".exe" if os.name == "nt" else ""
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
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    connection = await asyncpg.connect(host="127.0.0.1", port=ports["pg18"],
                        user=role, password=password, database="postgres", timeout=1)
                    try:
                        await connection.execute(f"CREATE DATABASE {databases['crm']} OWNER {role} ENCODING 'UTF8'")
                    finally:
                        await connection.close()
                    return
                except (OSError, asyncpg.PostgresError):
                    await asyncio.sleep(.1)
            raise RuntimeError("Disposable PostgreSQL18 readiness failed")
        asyncio.run(prepare_crm())
        asyncio.run(execute())
    except Exception as error:
        record["error_type"] = type(error).__name__
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
        for listener in listeners:
            listener.close()
        if pg_process and pg_process.poll() is None:
            subprocess.run([str(pg18_bin / ("pg_ctl" + exe)), "-D", str(pgdata),
                "stop", "-m", "fast", "-w", "-t", "15"], env=base_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            pg_process.wait(timeout=10)
        if pg_handle:
            pg_handle.close()
        record["pg18_stopped"] = pg_process is None or pg_process.poll() is not None
        record["source_unchanged"] = all(git(root, "rev-parse", "HEAD") == sources[name]["revision"]
            and not git(root, "status", "--porcelain") for name, root in roots.items())
        record["runtime_manifest_sha256"] = (hashlib.sha256((state / "runtime-manifests.jsonl").read_bytes()).hexdigest()
            if (state / "runtime-manifests.jsonl").exists() else None)
        if not all(record[key] for key in ("owned_database_cleanup", "pg18_database_dropped",
                "pg18_stopped", "source_unchanged")):
            record["status"] = "FAIL"
        (state / "summary.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
