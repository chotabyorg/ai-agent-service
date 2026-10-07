"""Authenticated-route helper close faults with real HTTP/1 pool/socket owners."""

import asyncio
import ipaddress
import json
import logging
import ssl
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import anyio
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from openai import AsyncOpenAI as RealOpenAI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import db.database as database_module
import main
import services.llm_service as llm_module
from api.dependencies import get_db
from core.config import get_settings
from tests.fakes import build_llm_response, build_tool_result
from tests.test_chat_flow import store_credentials
from tests.test_managed_turns import SENTINEL, execute, model, setup, tools


@pytest.fixture
def db_probe(api_client, db_session, db_engine, monkeypatch):
    """Use the real nested production provider against disposable PostgreSQL."""
    state = SimpleNamespace(sessions=[], checkouts=set(), drivers=[], close_fault=None,
                            close_started=asyncio.Event())

    class ObservedSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.sessions.append(self)

        async def close(self):
            if state.close_fault:
                state.close_started.set()
                if state.close_fault in {"deadline", "cancel"}:
                    await asyncio.sleep(2.1)
            await super().close()
            if state.close_fault == "raise":
                raise RuntimeError(SENTINEL)

    def checkout(connection, record, proxy):
        state.checkouts.add(record)
        state.drivers.append(connection.driver_connection)

    def checkin(connection, record):
        state.checkouts.discard(record)

    event.listen(db_engine.sync_engine, "checkout", checkout)
    event.listen(db_engine.sync_engine, "checkin", checkin)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", async_sessionmaker(
        bind=db_engine, class_=ObservedSession, expire_on_commit=False, autoflush=False))
    monkeypatch.setitem(main.app.dependency_overrides, get_db, get_db)
    try:
        yield state
    finally:
        event.remove(db_engine.sync_engine, "checkout", checkout)
        event.remove(db_engine.sync_engine, "checkin", checkin)


def tls_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-loopback")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "loopback-cert.pem", tmp_path / "loopback-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    return server_context, ssl.create_default_context(cafile=str(cert_path))


@asynccontextmanager
async def loopback_service(tmp_path, *, tls=False, proxy=False, post_tool=False, bad_kb=False, stall=False,
                           operation_error=None):
    state = SimpleNamespace(peers=[], calls=[], tasks=[], received=asyncio.Event())
    server_context, verify = tls_contexts(tmp_path) if tls else (None, True)

    async def serve(reader, writer):
        peer = SimpleNamespace(writer=writer, eof=asyncio.Event())
        state.peers.append(peer)
        state.tasks.append(asyncio.current_task())
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                if head.startswith(b"CONNECT "):
                    assert proxy and tls
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    await writer.start_tls(server_context)
                    continue
                headers = dict(line.lower().split(b": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line)
                body = await reader.readexactly(int(headers[b"content-length"]))
                state.calls.append((head.split(b"\r\n", 1)[0], json.loads(body)))
                if len(state.calls) == (2 if post_tool else 1):
                    state.received.set()
                    if operation_error:
                        if operation_error == "malformed":
                            writer.write(b"INVALID STATUS\r\n\r\n")
                            await writer.drain()
                        elif operation_error == "body":
                            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                         b"Content-Length: 100\r\n\r\n{")
                            await writer.drain()
                        assert await reader.read() == b""
                        peer.eof.set()
                        return
                if stall and len(state.calls) == (2 if post_tool else 1):
                    assert await reader.read() == b""
                    peer.eof.set()
                    return
                if b"/retrieval/" in head:
                    value = {"contexts": [], "total": 0}
                    status = b"503 Unavailable" if bad_kb else b"200 OK"
                else:
                    message = {"role": "assistant", "content": "safe draft", "tool_calls": None}
                    if post_tool and len(state.calls) == 1:
                        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "fake-call", "type": "function",
                            "function": {"name": "mutate", "arguments": "{}"}}]}
                    value, status = {"id": "synthetic-completion", "object": "chat.completion", "created": 1,
                        "model": "synthetic", "choices": [{"index": 0, "finish_reason": "stop", "message": message}]}, b"200 OK"
                encoded = json.dumps(value).encode()
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(encoded)).encode() + b"\r\nConnection: keep-alive\r\n\r\n" + encoded)
                await writer.drain()
        except asyncio.IncompleteReadError as exc:
            assert exc.partial == b""
            peer.eof.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ssl.SSLError, ConnectionError):
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=server_context if not proxy else None)
    state.origin = ("https" if tls and not proxy else "http") + "://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    state.verify = verify
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        for peer in state.peers:
            peer.writer.close()
        for task in state.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)


def bare_stream(connection):
    protocol = connection
    while getattr(protocol, "_connection", None) is not None:
        protocol = protocol._connection
    stream = protocol._network_stream._stream
    while hasattr(stream, "transport_stream"):
        stream = stream.transport_stream
    return stream


def helper_clients(monkeypatch, server, mode, *, fault_client=1, patch=True):
    original_client, original_transport = httpx.AsyncClient, httpx.AsyncHTTPTransport
    state = SimpleNamespace(clients=[], transports=[], connections=[], sockets=[], events=[],
                            close_started=asyncio.Event(), close_cancelled=False)

    class FaultTransport(original_transport):
        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            for connection in self._pool.connections:
                stream = bare_stream(connection)
                original_close = stream.aclose
                async def observed_close(close=original_close):
                    state.events.append(asyncio.current_task())
                    await close()
                stream.aclose = observed_close
            return response

        async def aclose(self):
            state.events.append(asyncio.current_task())
            if self.fault:
                state.close_started.set()
                state.connections.extend(self._pool.connections)
                for connection in self._pool.connections:
                    stream = bare_stream(connection)
                    state.sockets.append(stream.extra(anyio.abc.SocketAttribute.raw_socket))
                if mode == "raise":
                    logging.getLogger("httpx").warning(SENTINEL)
                    raise RuntimeError(SENTINEL)
                if mode == "deadline":
                    try:
                        await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        state.close_cancelled = True
                        raise
                if mode == "pool_removed":
                    # Real httpcore clears its list before this close path fails.
                    async def fail_connections(connections):
                        raise RuntimeError(SENTINEL)
                    self._pool._close_connections = fail_connections
                if mode == "stream_raise":
                    # HTTP/1 state becomes CLOSED before its actual stream fails.
                    for connection in self._pool.connections:
                        async def fail_stream():
                            raise RuntimeError(SENTINEL)
                        connection._connection._network_stream.aclose = fail_stream
            await super().aclose()

    def create(**kwargs):
        assert kwargs.get("trust_env") is False
        kwargs.pop("transport", None)
        transport = FaultTransport(verify=server.verify, trust_env=False)
        transport.fault = len(state.clients) + 1 == fault_client and mode != "success"
        client = original_client(transport=transport, **kwargs)
        state.clients.append(client)
        state.transports.append(transport)
        return client

    state.create = create
    if patch:
        monkeypatch.setattr(llm_module.httpx, "AsyncClient", create)
    return state


def direct_sdk(monkeypatch, state):
    state.sdks, state.sdk_closes = [], []

    def create_sdk(**kwargs):
        assert kwargs["max_retries"] == 0
        if hasattr(state, "operation_timeout"):
            kwargs["timeout"] = state.operation_timeout
        client = state.create(timeout=kwargs["timeout"], trust_env=False)
        sdk = RealOpenAI(**kwargs, http_client=client)
        original_close = sdk.close
        async def observed_close():
            state.sdk_closes.append(asyncio.current_task())
            await original_close()
        sdk.close = observed_close
        state.sdks.append(sdk)
        return sdk

    monkeypatch.setattr(llm_module, "AsyncOpenAI", create_sdk)


async def assert_released(state, server, before, db_probe):
    # Observe peer EOF before fixture cleanup can close any server resource.
    assert server.peers
    for peer in server.peers:
        await asyncio.wait_for(peer.eof.wait(), .5)
    await asyncio.gather(*server.tasks)
    assert all(client.is_closed for client in state.clients)
    assert all(not transport._pool.connections for transport in state.transports)
    assert all(connection.is_closed() for connection in state.connections)
    assert all(sock.fileno() == -1 for sock in state.sockets)
    assert state.events and len(set(state.events)) == 1
    assert state.events[0].get_name() == "managed-turn-owner"
    assert db_probe.sessions and all(not session.in_transaction() for session in db_probe.sessions)
    assert not db_probe.checkouts
    assert db_probe.drivers and all(driver.is_closed() for driver in db_probe.drivers)
    assert not (set(asyncio.all_tasks()) - before - {asyncio.current_task()})


def operation_close_fault(monkeypatch, state, mode, *, fault_connection=1):
    """Inject before protocol setup/operation cleanup, not at final aclose."""
    from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
    original = AnyIOBackend.connect_tcp
    original_tls = AnyIOStream.start_tls

    def fault_stream(stream, fault):
        close = stream.aclose
        async def native_close():
            state.events.append(asyncio.current_task())
            state.close_started.set()
            if fault and mode == "raise":
                raise RuntimeError(SENTINEL)
            if fault and mode == "deadline":
                await asyncio.sleep(5)
            await close()
        stream.aclose = native_close
        stream._goal2_test_fault = fault
        return stream

    async def connect(backend, *args, **kwargs):
        stream = await original(backend, *args, **kwargs)
        state.sockets.append(stream.get_extra_info("socket"))
        return fault_stream(stream, len(state.sockets) == fault_connection)

    async def start_tls(stream, *args, **kwargs):
        tls = await original_tls(stream, *args, **kwargs)
        return fault_stream(tls, getattr(stream, "_goal2_test_fault", False))

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    monkeypatch.setattr(AnyIOStream, "start_tls", start_tls)


@asynccontextmanager
async def stalled_tls_service(*, proxy=False):
    state = SimpleNamespace(peers=[], tasks=[], received=asyncio.Event(), verify=True)
    async def serve(reader, writer):
        peer = SimpleNamespace(writer=writer, eof=asyncio.Event())
        state.peers.append(peer)
        state.tasks.append(asyncio.current_task())
        try:
            if proxy:
                assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"CONNECT ")
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            hello = await reader.read(4096)
            assert hello and hello[0] == 22
            state.received.set()
            while await reader.read(4096):
                pass
            peer.eof.set()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    state.origin = ("http" if proxy else "https") + "://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        for peer in state.peers:
            peer.writer.close()
        for task in state.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)


@pytest.mark.integration
@pytest.mark.parametrize("family", ["kb", "privacy", "direct", "connect_proxy"])
@pytest.mark.parametrize("mode", ["work_deadline", "caller_cancel", "connect_deadline"])
async def test_native_tls_partial_acquisition_retained_before_protocol_and_cancel(
        api_client, db_session, db_probe, monkeypatch, model, tmp_path, family, mode):
    from httpcore._backends.anyio import AnyIOBackend
    _, value = await setup(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-helper-token")
    monkeypatch.setattr(settings, "knowledge_base_enabled", family == "kb")
    monkeypatch.setattr(settings, "privacy_filter_enabled", family == "privacy")
    monkeypatch.setattr(settings, "managed_ai_execution_seconds", 1)
    timeout = .05 if mode == "connect_deadline" else 5
    monkeypatch.setattr(settings, "knowledge_base_timeout_seconds", timeout)
    monkeypatch.setattr(settings, "privacy_filter_timeout_seconds", timeout)
    async with stalled_tls_service(proxy=family == "connect_proxy") as server:
        monkeypatch.setattr(settings, "knowledge_base_service_url", server.origin)
        monkeypatch.setattr(settings, "privacy_filter_base_url", server.origin)
        monkeypatch.setattr(settings, "llm_base_url", server.origin + "/v1")
        state = helper_clients(monkeypatch, server, "success", patch=family not in {"direct", "connect_proxy"})
        if family == "direct":
            state.operation_timeout = timeout
            direct_sdk(monkeypatch, state)
        elif family == "connect_proxy":
            monkeypatch.setenv("HTTPS_PROXY", server.origin)
            monkeypatch.setenv("HTTP_PROXY", server.origin)
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv("ALL_PROXY", "")
            monkeypatch.setattr(settings, "llm_base_url", "https://127.0.0.1:1/v1")
            def create_sdk(**kwargs):
                kwargs["timeout"] = timeout
                sdk = RealOpenAI(**kwargs)
                state.clients.append(sdk._client)
                state.transports = list({id(t): t for t in [sdk._client._transport, *sdk._client._mounts.values()]
                                         if t is not None}.values())
                return sdk
            monkeypatch.setattr(llm_module, "AsyncOpenAI", create_sdk)
        original_connect = AnyIOBackend.connect_tcp
        async def observe(backend, *args, **kwargs):
            stream = await original_connect(backend, *args, **kwargs)
            state.sockets.append(stream.get_extra_info("socket"))
            state.events.append(asyncio.current_task())
            return stream
        monkeypatch.setattr(AnyIOBackend, "connect_tcp", observe)
        before = set(asyncio.all_tasks())
        request = asyncio.create_task(execute(api_client, value))
        if mode == "caller_cancel":
            await asyncio.wait_for(server.received.wait(), 2)
            request.cancel("original partial TLS cancellation")
            await asyncio.sleep(0)
            request.cancel("repeated partial TLS cancellation")
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await request
            assert cancelled.value.args == ("original partial TLS cancellation",)
        else:
            if family == "kb" and mode == "connect_deadline":
                model.chat.completions.queue(build_llm_response(content="safe draft"))
            result = (await request).json()
            if family == "kb" and mode == "connect_deadline":
                assert result["status"] == "DRAFT_READY"
            else:
                assert result["status"] == "FAILED" and result["draft_text"] is None
                assert result["error_code"] == ("EXECUTION_TIMEOUT" if mode == "work_deadline" else
                                                "MODEL_TIMEOUT" if family in {"direct", "connect_proxy"} else
                                                "PRIVACY_UNAVAILABLE")
        assert len(server.peers) == len(state.sockets) == 1
        await assert_released(state, server, before, db_probe)


@pytest.mark.integration
@pytest.mark.parametrize("family,post_tool", [("kb", False), ("privacy", False), ("privacy", True),
                                             ("direct", False), ("direct", True)])
@pytest.mark.parametrize("operation_error", ["malformed", "read", "body"])
@pytest.mark.parametrize("close_fault", ["raise", "deadline"])
@pytest.mark.parametrize("tls", [False, True])
async def test_operation_close_failure_before_pool_snapshot_retains_actual_socket(
        api_client, db_session, db_probe, monkeypatch, model, tools, tmp_path, caplog, capfd,
        family, post_tool, operation_error, close_fault, tls):
    agent, value = await setup(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-helper-token")
    monkeypatch.setattr(settings, "knowledge_base_enabled", family == "kb")
    monkeypatch.setattr(settings, "privacy_filter_enabled", family == "privacy")
    monkeypatch.setattr(settings, "knowledge_base_timeout_seconds", .05)
    monkeypatch.setattr(settings, "privacy_filter_timeout_seconds", .05)
    if post_tool:
        await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                                owner_id=UUID(agent["user_id"]))
        tools.result = build_tool_result('{"ok": true}')
    model.chat.completions.queue(build_llm_response(content="must remain unconsumed"))
    async with loopback_service(tmp_path, post_tool=post_tool, operation_error=operation_error, tls=tls) as server:
        monkeypatch.setattr(settings, "knowledge_base_service_url", server.origin)
        monkeypatch.setattr(settings, "privacy_filter_base_url", server.origin)
        monkeypatch.setattr(settings, "llm_base_url", server.origin + "/v1")
        state = helper_clients(monkeypatch, server, "success", patch=family != "direct")
        if family == "direct":
            state.operation_timeout = .05
            direct_sdk(monkeypatch, state)
        operation_close_fault(monkeypatch, state, close_fault,
                              fault_connection=2 if family == "privacy" and post_tool else 1)
        before = set(asyncio.all_tasks())
        response = await execute(api_client, value)
        result = response.json()
        assert result["status"] == ("UNKNOWN" if post_tool else "FAILED")
        assert result["error_code"] == "TOOL_CLEANUP_FAILED"
        assert result["draft_text"] is None and result["operation_refs"] == []
        assert len(server.calls) == (2 if post_tool else 1)
        assert len(tools.calls) == int(post_tool) and model.chat.completions.calls == []
        await assert_released(state, server, before, db_probe)
        captured = capfd.readouterr()
        assert SENTINEL not in response.text + caplog.text + captured.out + captured.err


@pytest.mark.integration
@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("operation_error", ["malformed", "read", "body"])
async def test_native_proxy_mount_operation_close_loss_retains_forward_and_connect_socket(
        api_client, db_session, db_probe, monkeypatch, tmp_path, tls, operation_error):
    _, value = await setup(api_client, db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    async with loopback_service(tmp_path, tls=tls, proxy=True, operation_error=operation_error) as server:
        monkeypatch.setenv("HTTP_PROXY", server.origin)
        monkeypatch.setenv("HTTPS_PROXY", server.origin)
        monkeypatch.setenv("ALL_PROXY", "")
        monkeypatch.setenv("NO_PROXY", "")
        if tls:
            monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "loopback-cert.pem"))
        monkeypatch.setattr(get_settings(), "llm_base_url", ("https" if tls else "http") + "://127.0.0.1:1/v1")
        state = SimpleNamespace(clients=[], transports=[], connections=[], sockets=[], events=[],
                                close_started=asyncio.Event())
        def create_sdk(**kwargs):
            kwargs["timeout"] = .05
            sdk = RealOpenAI(**kwargs)
            state.clients.append(sdk._client)
            state.transports = list({id(t): t for t in [sdk._client._transport, *sdk._client._mounts.values()]
                                     if t is not None}.values())
            return sdk
        monkeypatch.setattr(llm_module, "AsyncOpenAI", create_sdk)
        operation_close_fault(monkeypatch, state, "raise")
        before = set(asyncio.all_tasks())
        result = (await execute(api_client, value)).json()
        assert result["status"] == "FAILED" and result["error_code"] == "TOOL_CLEANUP_FAILED"
        assert result["draft_text"] is None and result["operation_refs"] == []
        assert len(server.calls) == 1
        await assert_released(state, server, before, db_probe)


@pytest.mark.integration
@pytest.mark.parametrize("family,post_tool", [("kb", False), ("privacy", False), ("privacy", True)])
@pytest.mark.parametrize("mode", ["raise", "deadline", "pool_removed", "stream_raise"])
async def test_helper_fault_stops_draft_and_joins_real_pool_socket(api_client, db_session, monkeypatch, db_probe,
        model, tools, tmp_path, caplog, capfd, family, post_tool, mode):
    agent, value = await setup(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-helper-token")
    monkeypatch.setattr(settings, "knowledge_base_enabled", family == "kb")
    monkeypatch.setattr(settings, "privacy_filter_enabled", family == "privacy")
    if post_tool:
        await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                                owner_id=UUID(agent["user_id"]))
        tools.result = build_tool_result('{"ok": true}')
    model.chat.completions.queue(build_llm_response(content="must remain unconsumed"))
    async with loopback_service(tmp_path, post_tool=post_tool) as server:
        monkeypatch.setattr(settings, "knowledge_base_service_url", server.origin)
        monkeypatch.setattr(settings, "privacy_filter_base_url", server.origin)
        state = helper_clients(monkeypatch, server, mode, fault_client=2 if post_tool else 1)
        before, started = set(asyncio.all_tasks()), time.monotonic()
        with caplog.at_level(logging.DEBUG):
            response = await execute(api_client, value)
        assert time.monotonic() - started < 5
        result = response.json()
        assert result["status"] == ("UNKNOWN" if post_tool else "FAILED")
        assert result["error_code"] == "TOOL_CLEANUP_FAILED"
        assert result["draft_text"] is None and result["operation_refs"] == []
        assert len(server.calls) == (2 if post_tool else 1)
        assert model.chat.completions.calls == []
        assert len(tools.calls) == int(post_tool)
        if post_tool:
            assert [item["role"] for item in server.calls[1][1]["chat_completion"]["messages"]][-2:] == ["assistant", "tool"]
        if mode == "deadline":
            assert state.close_cancelled
        await assert_released(state, server, before, db_probe)
        captured = capfd.readouterr()
        assert SENTINEL not in response.text + caplog.text + captured.out + captured.err


@pytest.mark.integration
@pytest.mark.parametrize("family,post_tool", [("kb", False), ("privacy", False), ("privacy", True)])
@pytest.mark.parametrize("during", ["request", "close"])
async def test_caller_cancel_joins_helper_and_preserves_original_cancellation(api_client, db_session, monkeypatch, db_probe,
        model, tools, tmp_path, caplog, capfd, family, post_tool, during):
    agent, value = await setup(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-helper-token")
    monkeypatch.setattr(settings, "knowledge_base_enabled", family == "kb")
    monkeypatch.setattr(settings, "privacy_filter_enabled", family == "privacy")
    if post_tool:
        await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                                owner_id=UUID(agent["user_id"]))
        tools.result = build_tool_result('{"ok": true}')
    async with loopback_service(tmp_path, stall=during == "request", post_tool=post_tool) as server:
        monkeypatch.setattr(settings, "knowledge_base_service_url", server.origin)
        monkeypatch.setattr(settings, "privacy_filter_base_url", server.origin)
        state = helper_clients(monkeypatch, server, "deadline" if during == "close" else "success",
                               fault_client=2 if post_tool else 1)
        before, started = set(asyncio.all_tasks()), time.monotonic()
        request = asyncio.create_task(execute(api_client, value))
        event = state.close_started if during == "close" else server.received
        await asyncio.wait_for(event.wait(), 2)
        request.cancel("original cancellation")
        await asyncio.sleep(.03)
        if not request.done():
            request.cancel("repeated cancellation")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await request
        assert cancelled.value.args == ("original cancellation",)
        assert time.monotonic() - started < 5
        assert len(server.calls) == (2 if post_tool else 1) and model.chat.completions.calls == []
        assert len(tools.calls) == int(post_tool)
        await assert_released(state, server, before, db_probe)
        captured = capfd.readouterr()
        assert SENTINEL not in caplog.text + captured.out + captured.err


@pytest.mark.integration
@pytest.mark.parametrize("family", ["kb", "privacy"])
async def test_tls_fault_joins_actual_underlying_socket(api_client, db_session, monkeypatch, db_probe, model,
                                                       tmp_path, family):
    _, value = await setup(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-helper-token")
    monkeypatch.setattr(settings, "knowledge_base_enabled", family == "kb")
    monkeypatch.setattr(settings, "privacy_filter_enabled", family == "privacy")
    async with loopback_service(tmp_path, tls=True) as server:
        monkeypatch.setattr(settings, "knowledge_base_service_url", server.origin)
        monkeypatch.setattr(settings, "privacy_filter_base_url", server.origin)
        state = helper_clients(monkeypatch, server, "raise")
        before = set(asyncio.all_tasks())
        result = (await execute(api_client, value)).json()
        assert result["status"] == "FAILED" and result["error_code"] == "TOOL_CLEANUP_FAILED"
        assert result["draft_text"] is None and model.chat.completions.calls == []
        await assert_released(state, server, before, db_probe)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["http_error", "timeout"])
async def test_real_kb_operation_error_remains_fail_open_after_confirmed_close(api_client, db_session, db_probe,
        monkeypatch, model, tmp_path, mode):
    _, value = await setup(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-helper-token")
    monkeypatch.setattr(settings, "knowledge_base_enabled", True)
    if mode == "timeout":
        monkeypatch.setattr(settings, "knowledge_base_timeout_seconds", .1)
    model.chat.completions.queue(build_llm_response(content="safe draft"))
    async with loopback_service(tmp_path, bad_kb=mode == "http_error", stall=mode == "timeout") as server:
        monkeypatch.setattr(settings, "knowledge_base_service_url", server.origin)
        state = helper_clients(monkeypatch, server, "success")
        before = set(asyncio.all_tasks())
        result = (await execute(api_client, value)).json()
        assert result["status"] == "DRAFT_READY" and result["draft_text"] == "safe draft"
        assert len(model.chat.completions.calls) == 1
        assert [item["role"] for item in model.chat.completions.calls[0]["messages"]] == ["system", "user"]
        await assert_released(state, server, before, db_probe)


@pytest.mark.integration
@pytest.mark.parametrize("post_tool", [False, True])
@pytest.mark.parametrize("mode", ["raise", "deadline", "pool_removed", "stream_raise"])
async def test_real_direct_sdk_close_fault_joins_pool_socket_without_resend(api_client, db_session, db_probe,
        monkeypatch, tools, tmp_path, caplog, capfd, post_tool, mode):
    agent, value = await setup(api_client, db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    if post_tool:
        await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                                owner_id=UUID(agent["user_id"]))
        tools.result = build_tool_result('{"ok": true}')
    async with loopback_service(tmp_path, post_tool=post_tool) as server:
        monkeypatch.setattr(get_settings(), "llm_base_url", server.origin + "/v1")
        state = helper_clients(monkeypatch, server, mode, patch=False)
        direct_sdk(monkeypatch, state)
        before, started = set(asyncio.all_tasks()), time.monotonic()
        with caplog.at_level(logging.DEBUG):
            response = await execute(api_client, value)
        assert time.monotonic() - started < 5
        result = response.json()
        assert result["status"] == ("UNKNOWN" if post_tool else "FAILED")
        assert result["error_code"] == "TOOL_CLEANUP_FAILED"
        assert result["draft_text"] is None and result["operation_refs"] == []
        assert len(server.calls) == (2 if post_tool else 1)
        assert len(tools.calls) == int(post_tool)
        assert len(state.sdk_closes) == 1 and state.sdk_closes[0].get_name() == "managed-turn-owner"
        assert all(sdk.is_closed() for sdk in state.sdks)
        if mode == "deadline":
            assert state.close_cancelled
        await assert_released(state, server, before, db_probe)
        captured = capfd.readouterr()
        assert SENTINEL not in response.text + caplog.text + captured.out + captured.err


@pytest.mark.integration
@pytest.mark.parametrize("post_tool", [False, True])
@pytest.mark.parametrize("during", ["request", "close"])
async def test_direct_caller_cancel_joins_sdk_and_preserves_cancellation(api_client, db_session, monkeypatch, db_probe,
        tools, tmp_path, caplog, capfd, post_tool, during):
    agent, value = await setup(api_client, db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    if post_tool:
        await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                                owner_id=UUID(agent["user_id"]))
        tools.result = build_tool_result('{"ok": true}')
    async with loopback_service(tmp_path, post_tool=post_tool, stall=during == "request") as server:
        monkeypatch.setattr(get_settings(), "llm_base_url", server.origin + "/v1")
        state = helper_clients(monkeypatch, server, "deadline" if during == "close" else "success", patch=False)
        direct_sdk(monkeypatch, state)
        before, started = set(asyncio.all_tasks()), time.monotonic()
        request = asyncio.create_task(execute(api_client, value))
        event = state.close_started if during == "close" else server.received
        await asyncio.wait_for(event.wait(), 2)
        request.cancel("original direct cancellation")
        await asyncio.sleep(.03)
        if not request.done():
            request.cancel("repeated direct cancellation")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await request
        assert cancelled.value.args == ("original direct cancellation",)
        assert time.monotonic() - started < 5
        assert len(server.calls) == (2 if post_tool else 1) and len(tools.calls) == int(post_tool)
        assert len(state.sdk_closes) == 1
        await assert_released(state, server, before, db_probe)
        captured = capfd.readouterr()
        assert SENTINEL not in caplog.text + captured.out + captured.err


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["raise", "deadline"])
async def test_direct_tls_fault_joins_underlying_socket(api_client, db_session, monkeypatch, db_probe, tmp_path, mode):
    _, value = await setup(api_client, db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    async with loopback_service(tmp_path, tls=True) as server:
        monkeypatch.setattr(get_settings(), "llm_base_url", server.origin + "/v1")
        state = helper_clients(monkeypatch, server, mode, patch=False)
        direct_sdk(monkeypatch, state)
        before = set(asyncio.all_tasks())
        result = (await execute(api_client, value)).json()
        assert result["status"] == "FAILED" and result["error_code"] == "TOOL_CLEANUP_FAILED"
        assert result["draft_text"] is None and len(server.calls) == 1
        await assert_released(state, server, before, db_probe)


@pytest.mark.integration
@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("mode", ["raise", "deadline"])
async def test_sdk_environment_proxy_mounted_pool_is_owned_and_joined(api_client, db_session, monkeypatch, db_probe,
        tmp_path, tls, mode):
    _, value = await setup(api_client, db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    async with loopback_service(tmp_path, tls=tls, proxy=True) as server:
        monkeypatch.setenv("HTTP_PROXY", server.origin)
        monkeypatch.setenv("HTTPS_PROXY", server.origin)
        monkeypatch.setenv("ALL_PROXY", "")
        monkeypatch.setenv("NO_PROXY", "")
        if tls:
            monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "loopback-cert.pem"))
        # Port1 has no listener; the actual local proxy supplies the completion.
        monkeypatch.setattr(get_settings(), "llm_base_url", ("https" if tls else "http") + "://127.0.0.1:1/v1")
        state = SimpleNamespace(clients=[], transports=[], connections=[], sockets=[], events=[], sdk_closes=[])

        def create_sdk(**kwargs):
            sdk = RealOpenAI(**kwargs)
            client = sdk._client
            state.clients.append(client)
            state.transports = list({id(t): t for t in [client._transport, *client._mounts.values()] if t is not None}.values())
            for transport in state.transports:
                original_handle, original_close = transport.handle_async_request, transport.aclose
                async def observed_request(request, original=original_handle, owned=transport):
                    response = await original(request)
                    for connection in owned._pool.connections:
                        state.connections.append(connection)
                        stream = bare_stream(connection)
                        state.sockets.append(stream.extra(anyio.abc.SocketAttribute.raw_socket))
                        close = stream.aclose
                        async def observed_socket_close(original_socket_close=close):
                            state.events.append(asyncio.current_task())
                            await original_socket_close()
                        stream.aclose = observed_socket_close
                    return response
                async def fault_close(original=original_close, owned=transport):
                    state.events.append(asyncio.current_task())
                    if owned._pool.connections:
                        if mode == "raise":
                            raise RuntimeError(SENTINEL)
                        await asyncio.sleep(5)
                    await original()
                transport.handle_async_request, transport.aclose = observed_request, fault_close
            close_sdk = sdk.close
            async def observed_sdk_close():
                state.sdk_closes.append(asyncio.current_task())
                await close_sdk()
            sdk.close = observed_sdk_close
            return sdk

        monkeypatch.setattr(llm_module, "AsyncOpenAI", create_sdk)
        before, started = set(asyncio.all_tasks()), time.monotonic()
        result = (await execute(api_client, value)).json()
        assert time.monotonic() - started < 5
        assert result["status"] == "FAILED" and result["error_code"] == "TOOL_CLEANUP_FAILED"
        assert result["draft_text"] is None and len(server.calls) == 1
        assert state.connections and type(state.connections[0]).__name__ == (
            "AsyncTunnelHTTPConnection" if tls else "AsyncForwardHTTPConnection")
        assert len(state.sdk_closes) == 1
        await assert_released(state, server, before, db_probe)


@pytest.mark.integration
@pytest.mark.parametrize("mode,post_tool", [("raise", False), ("raise", True),
                                            ("deadline", False), ("deadline", True), ("cancel", False)])
async def test_final_production_db_exit_joins_close_and_preserves_failure_or_cancellation(
        api_client, db_session, monkeypatch, db_probe, tools, tmp_path, caplog, capfd, mode, post_tool):
    agent, value = await setup(api_client, db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", False)
    if post_tool:
        await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                                owner_id=UUID(agent["user_id"]))
        tools.result = build_tool_result('{"ok": true}')
    async with loopback_service(tmp_path, post_tool=post_tool) as server:
        monkeypatch.setattr(get_settings(), "llm_base_url", server.origin + "/v1")
        state = helper_clients(monkeypatch, server, "success", patch=False)
        direct_sdk(monkeypatch, state)
        db_probe.close_fault = mode
        before, started = set(asyncio.all_tasks()), time.monotonic()
        request = asyncio.create_task(execute(api_client, value))
        if mode == "cancel":
            await asyncio.wait_for(db_probe.close_started.wait(), 2)
            request.cancel("original database exit cancellation")
            await asyncio.sleep(.03)
            request.cancel("repeated database exit cancellation")
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await request
            assert cancelled.value.args == ("original database exit cancellation",)
        else:
            response = await request
            result = response.json()
            assert result["status"] == ("UNKNOWN" if post_tool else "FAILED")
            assert result["error_code"] == "TOOL_CLEANUP_FAILED"
            assert result["draft_text"] is None and result["operation_refs"] == []
            assert SENTINEL not in response.text
        assert time.monotonic() - started < 5
        assert len(server.calls) == (2 if post_tool else 1) and len(tools.calls) == int(post_tool)
        assert len(state.sdk_closes) == 1
        await assert_released(state, server, before, db_probe)
        captured = capfd.readouterr()
        assert SENTINEL not in caplog.text + captured.out + captured.err
