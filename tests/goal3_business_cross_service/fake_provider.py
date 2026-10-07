"""Independent fake provider/model HTTP process with PostgreSQL evidence.

The provider owns its own database; its receipts, keys, objects and counters
survive both application crashes and its own restart. It never calls a provider.
"""

import asyncio
import json
import os
import select
import socket
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from tests.goal3_business_cross_service.network import install_guard, self_check

DDL = """
CREATE TABLE IF NOT EXISTS fake_requests(
 sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind text NOT NULL,
 operation_id text, body jsonb NOT NULL, created_at timestamptz DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS fake_receipts(
 operation_id text PRIMARY KEY, native_key text UNIQUE, fingerprint text NOT NULL,
 receipt jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS fake_objects(
 object_id text PRIMARY KEY, operation_id text NOT NULL);
CREATE TABLE IF NOT EXISTS fake_facts(
 id integer PRIMARY KEY CHECK(id=1), values jsonb NOT NULL, revisions jsonb NOT NULL);
INSERT INTO fake_facts VALUES(1,'{}','{"catalog":1}') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS fake_key_authorities(native_key text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS fake_keys(
 native_key text PRIMARY KEY REFERENCES fake_key_authorities(native_key),
 fingerprint text NOT NULL, receipt jsonb NOT NULL, expires_at timestamptz);
ALTER TABLE fake_requests ADD COLUMN IF NOT EXISTS accepted_at timestamptz;
ALTER TABLE fake_requests ADD COLUMN IF NOT EXISTS decision text;
CREATE TABLE IF NOT EXISTS fake_settings(key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS fake_model_queue(
 sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, script jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS fake_barriers(label text PRIMARY KEY, entered boolean DEFAULT false,
 released boolean DEFAULT false);
CREATE TABLE IF NOT EXISTS fake_connections(
 id text PRIMARY KEY, opened_at timestamptz DEFAULT clock_timestamp(), closed_at timestamptz);
ALTER TABLE fake_connections ADD COLUMN IF NOT EXISTS peer_eof_at timestamptz;
ALTER TABLE fake_barriers ADD COLUMN IF NOT EXISTS connection_id text;
"""


def completion(text="Fixture draft", calls=None):
    return {"id": "goal3-fixture", "object": "chat.completion", "created": 1,
            "model": "fixture-model", "choices": [{"index": 0, "finish_reason":
            "tool_calls" if calls else "stop", "message": {"role": "assistant",
            "content": text, "tool_calls": calls}}], "usage": {"prompt_tokens": 1,
            "completion_tokens": 1, "total_tokens": 2}}


def tool_call(name, arguments, identifier=None):
    return {"id": identifier or str(uuid4()), "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


class Provider:
    def __init__(self, config):
        self.config = config

    async def connection(self):
        import asyncpg
        return await asyncpg.connect(self.config["database"], timeout=5)

    async def initialize(self):
        from schemas.business import ProviderDispatch
        from services.business_registry import ExecutionContract
        self.dispatch_type = ProviderDispatch
        self.contract_revision = ExecutionContract(expected_facts="CONDITIONAL_EXPECTED_FACTS_V1",
            bounded_acceptance=self.config.get("profile", {}).get("bounded_acceptance", "PROVIDER_ACCEPT_BEFORE_V1")).revision
        db = await self.connection()
        try:
            assert 160000 <= int(await db.fetchval("SHOW server_version_num")) < 170000
            await db.execute(DDL)
        finally:
            await db.close()

    async def setting(self, db, key, default=None):
        value = await db.fetchval("SELECT value FROM fake_settings WHERE key=$1", key)
        return default if value is None else json.loads(value)

    async def barrier(self, label, connection_id=None, peer_closed=None):
        db = await self.connection()
        try:
            await db.execute("INSERT INTO fake_barriers(label,entered,connection_id) VALUES($1,true,$2) "
                             "ON CONFLICT(label) DO UPDATE SET entered=true,connection_id=EXCLUDED.connection_id",
                             label, connection_id)
            deadline = time.monotonic() + 40
            while not await db.fetchval("SELECT released FROM fake_barriers WHERE label=$1", label):
                if peer_closed is not None and peer_closed():
                    await db.execute("UPDATE fake_connections SET peer_eof_at=COALESCE(peer_eof_at,clock_timestamp()) "
                                     "WHERE id=$1", connection_id)
                if time.monotonic() > deadline:
                    raise RuntimeError("Owned fake barrier deadline")
                await asyncio.sleep(.02)
        finally:
            await db.close()

    async def request(self, method, path, body):
        db = await self.connection()
        try:
            if path == "/health":
                return 200, {"status": "ok"}, None
            if path == "/__test__/script" and method == "POST":
                async with db.transaction():
                    if "facts" in body:
                        # Same durable facts authority as provider acceptance.
                        await db.execute("SELECT id FROM fake_facts WHERE id=1 FOR UPDATE")
                        await db.execute("UPDATE fake_facts SET values=$1::jsonb,revisions=$2::jsonb WHERE id=1",
                                         json.dumps(body["facts"]["values"]), json.dumps(body["facts"]["revisions"]))
                    if body.get("clear_model", False):
                        await db.execute("DELETE FROM fake_model_queue")
                    for key, value in body.get("settings", {}).items():
                        await db.execute("INSERT INTO fake_settings VALUES($1,$2::jsonb) "
                                         "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                                         key, json.dumps(value))
                    for script in body.get("model", []):
                        await db.execute("INSERT INTO fake_model_queue(script) VALUES($1::jsonb)",
                                         json.dumps(script))
                    for label in body.get("release", []):
                        await db.execute("UPDATE fake_barriers SET released=true WHERE label=$1", label)
                return 200, {"ok": True}, None
            if path == "/v1/chat/completions" and method == "POST":
                async with db.transaction():
                    await db.execute("INSERT INTO fake_requests(kind,body) VALUES('MODEL',$1::jsonb)",
                                     json.dumps(body))
                    row = await db.fetchrow("DELETE FROM fake_model_queue WHERE sequence=(SELECT "
                                           "sequence FROM fake_model_queue ORDER BY sequence "
                                           "FOR UPDATE LIMIT 1) RETURNING script")
                script = json.loads(row[0]) if row else {"status": 500, "body": {"error": {
                    "message": "Synthetic model script exhausted", "type": "fixture"}}}
                return script.get("status", 200), script["body"], script.get("barrier")
            if path == "/facts":
                async with db.transaction():
                    row = await db.fetchrow("SELECT values,revisions FROM fake_facts WHERE id=1 FOR SHARE")
                    facts = {name: json.loads(row[name]) for name in ("values", "revisions")}
                    await db.execute("INSERT INTO fake_requests(kind,body) VALUES('FACTS',$1::jsonb)", json.dumps(facts))
                barrier = await self.setting(db, "facts_barrier")
                if barrier:
                    await self.barrier(barrier)
                return 200, facts, None
            if path == "/execute" and method == "POST":
                command = body.get("command", body)
                op, key, fp = command["operation_id"], command.get("provider_idempotency_key"), command["request_fingerprint"]
                script = await self.setting(db, "execute", {})
                slot = command["arguments"].get("slot", "")
                script = script.get(slot, script.get("default", {}))
                binding_names = ("scope", "operation_id", "request_fingerprint", "adapter_id",
                                 "connection_id", "provider_profile_revision", "issued_at",
                                 "provider_idempotency_key")
                binding = {name: command[name] for name in binding_names}
                # Count receipt of bytes before any acceptance wait. A timed-out
                # caller can still have a durable counted request waiting here.
                sequence = await db.fetchval("INSERT INTO fake_requests(kind,operation_id,body) "
                    "VALUES('POST',$1,$2::jsonb) RETURNING sequence", op, json.dumps(body))
                if script.get("before_accept"):
                    await self.barrier(script["before_accept"])
                async with db.transaction():
                    # Acquire every provider authority first, then reload facts
                    # and sample clock_timestamp at the acceptance decision.
                    await db.execute("SELECT id FROM fake_facts WHERE id=1 FOR UPDATE")
                    if key:
                        await db.execute("INSERT INTO fake_key_authorities VALUES($1) ON CONFLICT DO NOTHING", key)
                        await db.execute("SELECT native_key FROM fake_key_authorities WHERE native_key=$1 FOR UPDATE", key)
                    row = await db.fetchrow("SELECT values,revisions FROM fake_facts WHERE id=1")
                    current = {name: json.loads(row[name]) for name in ("values", "revisions")}
                    now = await db.fetchval("SELECT clock_timestamp()")
                    profile = self.config.get("profile", {})
                    deadline = body.get("accept_before")
                    reason = None
                    legacy_unenveloped = ("command" not in body and profile.get("retained_legacy") is True
                                          and profile.get("bounded_acceptance") == "FENCED")
                    if ("command" not in body or "attempt" not in body) and not legacy_unenveloped:
                        reason = "UNENVELOPED_ATTEMPT"
                    elif not legacy_unenveloped:
                        try:
                            self.dispatch_type.model_validate(body)
                            if body["execution_contract"] != self.contract_revision:
                                reason = "EXECUTION_CONTRACT_CONFLICT"
                        except (ValueError, TypeError):
                            reason = "ATTEMPT_CONFLICT"
                    if reason is None and key and profile.get("native_policy") == "BOUNDED" and profile.get(
                            "bounded_acceptance", "PROVIDER_ACCEPT_BEFORE_V1") == "PROVIDER_ACCEPT_BEFORE_V1":
                        original_deadline = datetime.fromisoformat(command["issued_at"].replace("Z", "+00:00")) + timedelta(seconds=profile["horizon"])
                        if deadline is None or datetime.fromisoformat(deadline.replace("Z", "+00:00")) != original_deadline:
                            reason = "DEADLINE_CONFLICT"
                        elif now >= original_deadline:
                            reason = "ACCEPTANCE_EXPIRED"
                    if reason is None and (command["facts"] != current["values"] or command["fact_revisions"] != current["revisions"]):
                        reason = "EXPECTED_FACTS_CHANGED"
                    original_row = await db.fetchrow("SELECT fingerprint,receipt FROM fake_receipts WHERE operation_id=$1", op)
                    original = json.loads(original_row["receipt"]) if original_row else None
                    if original is not None and (original_row["fingerprint"] != fp or original["binding"] != binding):
                        reason = reason or "ORIGINAL_BINDING_CONFLICT"
                    await db.execute("UPDATE fake_requests SET accepted_at=$1,decision=$2 WHERE sequence=$3", now, reason or "ACCEPTED", sequence)
                    if reason:
                        await db.execute("INSERT INTO fake_requests(kind,operation_id,body) VALUES('REJECT',$1,$2::jsonb)", op, json.dumps({"reason": reason}))
                        rejected = {"kind": "ATTEMPT_REJECTED", "binding": binding,
                                    "attempt": body.get("attempt"), "reason_code": reason}
                        return 200 if body.get("attempt") else 409, rejected, None
                    # Honest key expiry never deletes the separately durable
                    # original object or authoritative original receipt.
                    if key:
                        await db.execute("DELETE FROM fake_keys WHERE native_key=$1 AND expires_at<=$2", key, now)
                    if original is not None and original["kind"] == "FAILED_NO_EFFECT":
                        # Original no-effect is a permanent provider fence under
                        # this full binding. Cache expiry or a positive later
                        # script cannot invalidate already-issued final evidence.
                        # This is not success dedupe for LOOKUP/BLIND profiles.
                        await db.execute("INSERT INTO fake_requests(kind,operation_id,body) VALUES('ACCEPT',$1,'{}')", op)
                        return 200, original, None
                    old = await db.fetchrow("SELECT fingerprint,receipt FROM fake_keys WHERE native_key=$1", key) if key else None
                    if old and old["fingerprint"] != fp:
                        return 409, {"kind": "AMBIGUOUS", "reason_code": "KEY_CONFLICT"}, None
                    if old:
                        receipt = json.loads(old["receipt"])
                    else:
                        negative = script.get("negative", False)
                        if negative:
                            # An evicted cache proves nothing about the original
                            # logical operation. Existing durable B1 evidence
                            # cannot be overwritten by rejection of a later try.
                            if original is not None:
                                await db.execute("INSERT INTO fake_requests(kind,operation_id,body) VALUES('ACCEPT',$1,'{}')", op)
                                return 200, original, None
                        receipt = {"kind": "FAILED_NO_EFFECT" if negative else "SUCCEEDED",
                                   "binding": binding, "receipt_id": "r:" + op,
                                   "evidence": {"proof": "REJECTED" if negative else "CREATED"}}
                        if negative:
                            receipt["reason_code"] = "REJECTED"
                        else:
                            receipt["object_id"] = "b:" + str(uuid4())
                            await db.execute("INSERT INTO fake_objects VALUES($1,$2)", receipt["object_id"], op)
                            await db.execute("INSERT INTO fake_requests(kind,operation_id,body) VALUES('MUTATION',$1,'{}')", op)
                        await db.execute("INSERT INTO fake_receipts VALUES($1,$2,$3,$4::jsonb) "
                                         "ON CONFLICT(operation_id) DO NOTHING",
                                         op, key, fp, json.dumps(receipt))
                        if key:
                            expiry = now + timedelta(seconds=profile["horizon"]) if profile.get("native_policy") == "BOUNDED" else None
                            await db.execute("INSERT INTO fake_keys VALUES($1,$2,$3::jsonb,$4)", key, fp, json.dumps(receipt), expiry)
                    await db.execute("INSERT INTO fake_requests(kind,operation_id,body) VALUES('ACCEPT',$1,'{}')", op)
                # The effect/receipt/counter COMMIT positively returns before any loss/gate.
                return 200, receipt, script.get("barrier") or ("DROP" if script.get("drop") else None)
            if path.startswith("/operations/") and method == "GET":
                op = path.rsplit("/", 1)[1]
                await db.execute("INSERT INTO fake_requests(kind,operation_id,body) VALUES('LOOKUP',$1,'{}')", op)
                lookup = await self.setting(db, "lookup", "receipt")
                if lookup != "receipt":
                    return 200, {"kind": "AMBIGUOUS", "reason_code": "NO_AUTHORITATIVE_EVIDENCE"}, None
                value = await db.fetchval("SELECT receipt FROM fake_receipts WHERE operation_id=$1", op)
                return 200, json.loads(value) if value else {"kind": "AMBIGUOUS",
                    "reason_code": "NO_AUTHORITATIVE_EVIDENCE"}, None
            return 404, {"detail": "No synthetic route"}, None
        finally:
            await db.close()


def run():
    install_guard([("127.0.0.1", 55438)])
    self_check()
    config = json.loads(Path(os.environ["GOAL3_PROVIDER_CONFIG"]).read_text())
    provider = Provider(config)
    asyncio.run(provider.initialize())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection_id = str(uuid4())
            async def opened():
                db = await provider.connection()
                try:
                    await db.execute("INSERT INTO fake_connections(id) VALUES($1)", self.connection_id)
                finally:
                    await db.close()
            asyncio.run(opened())

        def finish(self):
            super().finish()
            async def closed():
                db = await provider.connection()
                try:
                    await db.execute("UPDATE fake_connections SET closed_at=clock_timestamp() WHERE id=$1",
                                     self.connection_id)
                finally:
                    await db.close()
            asyncio.run(closed())

        def handle_request(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            status, value, barrier = asyncio.run(provider.request(self.command, self.path, body))
            if barrier == "DROP":
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if barrier:
                def peer_closed():
                    try:
                        return bool(select.select([self.connection], [], [], 0)[0]) and self.connection.recv(1, socket.MSG_PEEK) == b""
                    except (ConnectionResetError, OSError):
                        return True
                asyncio.run(provider.barrier(barrier, self.connection_id, peer_closed))
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        do_GET = do_POST = handle_request

    listener = socket.fromfd(config["fd"], socket.AF_INET, socket.SOCK_STREAM)
    server = ThreadingHTTPServer(("127.0.0.1", config["port"]), Handler, bind_and_activate=False)
    server.socket.close()
    server.socket, server.server_address = listener, listener.getsockname()
    server.daemon_threads = True
    server.serve_forever(.05)


if __name__ == "__main__":
    run()
