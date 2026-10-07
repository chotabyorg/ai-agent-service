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
                await db.execute("INSERT INTO fake_requests(kind,body) VALUES('FACTS','{}')")
                return 200, {"values": {}, "revisions": {"catalog": 1}}, None
            if path == "/execute" and method == "POST":
                op, key, fp = body["operation_id"], body.get("provider_idempotency_key"), body["request_fingerprint"]
                script = await self.setting(db, "execute", {})
                slot = body["arguments"].get("slot", "")
                script = script.get(slot, script.get("default", {}))
                binding_names = ("scope", "operation_id", "request_fingerprint", "adapter_id",
                                 "connection_id", "provider_profile_revision", "issued_at",
                                 "provider_idempotency_key")
                binding = {name: body[name] for name in binding_names}
                async with db.transaction():
                    # Separate provider-key authority serializes simultaneous native requests.
                    await db.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", key or str(uuid4()))
                    await db.execute("INSERT INTO fake_requests(kind,operation_id,body) "
                                     "VALUES('POST',$1,$2::jsonb)", op, json.dumps(body))
                    old = await db.fetchrow("SELECT fingerprint,receipt FROM fake_receipts "
                                           "WHERE native_key=$1", key) if key else None
                    if old and old["fingerprint"] != fp:
                        return 409, {"kind": "AMBIGUOUS", "reason_code": "KEY_CONFLICT"}, None
                    if old:
                        receipt = json.loads(old["receipt"])
                    else:
                        negative = script.get("negative", False)
                        receipt = {"kind": "FAILED_NO_EFFECT" if negative else "SUCCEEDED",
                                   "binding": binding, "receipt_id": "r:" + op,
                                   "evidence": {"proof": "REJECTED" if negative else "CREATED"}}
                        if negative:
                            receipt["reason_code"] = "REJECTED"
                        else:
                            receipt["object_id"] = "b:" + str(uuid4())
                            await db.execute("INSERT INTO fake_objects VALUES($1,$2)", receipt["object_id"], op)
                        await db.execute("INSERT INTO fake_receipts VALUES($1,$2,$3,$4::jsonb) "
                                         "ON CONFLICT(operation_id) DO UPDATE SET receipt=EXCLUDED.receipt",
                                         op, key, fp, json.dumps(receipt))
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
