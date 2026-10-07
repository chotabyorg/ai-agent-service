"""Own-close checkpoint faults on actual pinned native owners through the route.

The fault seam writes a bounded synthetic native buffer to the captured transport
with a paused local peer, then interrupts ONLY the real native close checkpoint.
It does not delay/replace native aclose or claim default 2s scheduling frequency.
"""

import asyncio
import inspect
import json
import socket
import ssl
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import anyio
import httpx
import pytest
import anyio._backends._asyncio as native_asyncio

import services.managed_http_cleanup as cleanup
from core.config import get_settings
from tests.fakes import build_llm_response, build_tool_result
from tests.test_chat_flow import store_credentials
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_http_cleanup import db_probe, direct_sdk, helper_clients, tls_contexts
from tests.test_managed_turns import execute, model, setup, tools


@asynccontextmanager
async def paused_peer(tmp_path, *, post_tool=False, operation=False, tls=False, proxy=False):
    state = SimpleNamespace(peers=[], tasks=[], calls=[], target=2 if post_tool else 1,
                            armed=False, release=asyncio.Event(), reached=asyncio.Event())
    server_context, state.verify = tls_contexts(tmp_path) if tls else (None, True)

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
                state.calls.append(json.loads(body))
                target = len(state.calls) == state.target
                if target and operation:
                    state.reached.set()
                    await state.release.wait()
                    while await reader.read(65536):
                        pass
                    peer.eof.set()
                    return
                if b"/retrieval/" in head:
                    value = {"contexts": [], "total": 0}
                else:
                    message = {"role": "assistant", "content": "safe draft", "tool_calls": None}
                    if post_tool and not target:
                        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "native-close-call",
                            "type": "function", "function": {"name": "business_execute_plan", "arguments": plan_arguments()}}]}
                    value = {"id": "synthetic", "object": "chat.completion", "created": 1, "model": "synthetic",
                             "choices": [{"index": 0, "finish_reason": "stop", "message": message}]}
                encoded = json.dumps(value).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(encoded)).encode() + b"\r\n\r\n" + encoded)
                await writer.drain()
                if target:
                    state.reached.set()
                    await state.release.wait()
                    while await reader.read(65536):
                        pass
                    peer.eof.set()
                    return
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            # Buffered abort can produce EOF or reset; both are physical release.
            peer.eof.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    server = await asyncio.start_server(serve, sock=listener, ssl=server_context if not proxy else None, limit=4096)
    state.origin = ("https" if tls and not proxy else "http") + "://127.0.0.1:" + str(listener.getsockname()[1])
    try:
        yield state
    finally:
        # Only after test assertions; this cannot manufacture a joined client.
        for resource in getattr(state, "native_resources", []):
            resource.transport.abort()
        await asyncio.sleep(0)
        state.release.set()
        for peer in state.peers:
            if not peer.writer.transport.is_closing():
                peer.writer.transport.resume_reading()
            peer.writer.close()
        server.close()
        await server.wait_closed()
        for task in state.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)


def native_checkpoint_fault(monkeypatch, server, *, mode):
    """Observe native close's real close→checkpoint→abort without replacing it."""
    state = SimpleNamespace(resources=[], events=[], scope_events=[], checkpoint_events=[], armed=False,
                            close_started=asyncio.Event(), finish=asyncio.Event())
    original_close, original_scope, original_sleep = cleanup._OwnedStream.aclose, anyio.CancelScope, native_asyncio.sleep

    def scope_factory(*args, **kwargs):
        if state.armed and kwargs.get("shield") and "deadline" in kwargs:
            state.scope_events.append(asyncio.current_task())
            if mode == "deadline":
                kwargs["deadline"] = anyio.current_time()
        return original_scope(*args, **kwargs)

    async def checkpoint(seconds):
        # This dependency seam faults ONLY SocketStream.aclose's actual native
        # checkpoint, after the transport is closing; no pre-native await.
        frame = inspect.currentframe().f_back
        if (mode == "error" and state.armed and frame.f_code is native_asyncio.SocketStream.aclose.__code__
                and not state.checkpoint_events):
            state.checkpoint_events.append(asyncio.current_task())
            raise RuntimeError("synthetic-native-checkpoint-error")
        await original_sleep(seconds)

    async def observed_close(owned):
        raw = owned._stream
        while hasattr(raw, "transport_stream"):
            raw = raw.transport_stream
        if len(server.calls) == server.target and not state.armed:
            state.armed = server.armed = True
            transport, sock = raw._transport, raw._transport.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            for peer in server.peers:
                peer.writer.transport.pause_reading()
            transport.write(b"x" * 131072)
            assert transport.get_write_buffer_size() > 0
            state.resources.append(SimpleNamespace(raw=raw, transport=transport, socket=sock, fd=sock.fileno(),
                                                  queued=transport.get_write_buffer_size()))
            state.close_started.set()
        state.events.append(asyncio.current_task())
        try:
            await original_close(owned)
        finally:
            if state.armed:
                state.finish.set()

    monkeypatch.setattr(cleanup, "anyio", SimpleNamespace(CancelScope=scope_factory, current_time=anyio.current_time,
                                                         sleep=anyio.sleep))
    monkeypatch.setattr(native_asyncio, "sleep", checkpoint)
    monkeypatch.setattr(cleanup._OwnedStream, "aclose", observed_close)
    return state


@pytest.mark.integration
@pytest.mark.parametrize("family,post_tool", [("kb", False), ("privacy", False), ("privacy", True),
                                            ("direct", False), ("direct", True)])
@pytest.mark.parametrize("operation,mode", [(False, "deadline"), (True, "deadline"), (False, "error"),
                                         (False, "success")])
async def test_authenticated_native_buffered_close_joins_actual_owner(api_client, db_session, db_probe, monkeypatch,
        model, tools, tmp_path, family, post_tool, operation, mode):
    await run_native_case(api_client, db_session, db_probe, monkeypatch, model, tools, tmp_path,
                          family=family, post_tool=post_tool, operation=operation, mode=mode)


@pytest.mark.integration
@pytest.mark.parametrize("tls,proxy", [(True, False), (False, True), (True, True)])
async def test_direct_native_buffered_close_covers_tls_and_sdk_proxy_mount(api_client, db_session, db_probe, monkeypatch,
        model, tools, tmp_path, tls, proxy):
    await run_native_case(api_client, db_session, db_probe, monkeypatch, model, tools, tmp_path,
                          family="direct", mode="deadline", tls=tls, proxy=proxy)


@pytest.mark.integration
@pytest.mark.parametrize("repeat", [False, True])
async def test_native_buffered_join_preserves_first_caller_cancellation(api_client, db_session, db_probe, monkeypatch,
        model, tools, tmp_path, repeat):
    await run_native_case(api_client, db_session, db_probe, monkeypatch, model, tools, tmp_path,
                          family="direct", mode="deadline", operation=True, cancel=repeat)


async def run_native_case(api_client, db_session, db_probe, monkeypatch, model, tools, tmp_path, *,
                          family, mode, post_tool=False, operation=False, tls=False, proxy=False, cancel=None):
    agent, value = await setup(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "knowledge_base_enabled", family == "kb")
    monkeypatch.setattr(settings, "privacy_filter_enabled", family == "privacy")
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-native-close-token")
    if post_tool:
        await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"},
                                owner_id=UUID(agent["user_id"]))
        provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(content="safe draft"))
    async with paused_peer(tmp_path, post_tool=post_tool, operation=operation, tls=tls, proxy=proxy) as server:
        monkeypatch.setattr(settings, "knowledge_base_service_url", server.origin)
        monkeypatch.setattr(settings, "privacy_filter_base_url", server.origin)
        monkeypatch.setattr(settings, "privacy_filter_timeout_seconds", .1 if operation else 2)
        monkeypatch.setattr(settings, "knowledge_base_timeout_seconds", .1 if operation else 2)
        monkeypatch.setattr(settings, "llm_base_url", server.origin + "/v1")
        clients = helper_clients(monkeypatch, server, "success", patch=family != "direct")
        if family == "direct":
            clients.operation_timeout = .1 if operation else 2
            if proxy:
                # Actual SDK constructor defaults install forward/CONNECT mounts.
                from openai import AsyncOpenAI
                import services.llm_service as llm
                monkeypatch.setenv("HTTP_PROXY", server.origin)
                monkeypatch.setenv("HTTPS_PROXY", server.origin)
                monkeypatch.setenv("ALL_PROXY", "")
                monkeypatch.setenv("NO_PROXY", "")
                if tls:
                    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "loopback-cert.pem"))
                monkeypatch.setattr(settings, "llm_base_url", ("https" if tls else "http") + "://127.0.0.1:1/v1")
                def sdk(**kwargs):
                    value = AsyncOpenAI(**kwargs)
                    clients.clients.append(value._client)
                    clients.transports.extend(t for t in [value._client._transport, *value._client._mounts.values()]
                                              if t is not None)
                    return value
                monkeypatch.setattr(llm, "AsyncOpenAI", sdk)
            else:
                direct_sdk(monkeypatch, clients)
        fault = native_checkpoint_fault(monkeypatch, server, mode=mode)
        server.native_resources = fault.resources
        before = set(asyncio.all_tasks())
        request = asyncio.create_task(execute(api_client, value))
        if cancel is not None:
            await asyncio.wait_for(server.reached.wait(), 2)
            request.cancel("first native close cancellation")
            await asyncio.wait_for(fault.close_started.wait(), 2)
            if cancel:
                request.cancel("repeated native close cancellation")
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await request
            assert cancelled.value.args == ("first native close cancellation",)
        else:
            result = (await request).json()
            assert result["status"] == ("DRAFT_READY" if mode == "success" else "UNKNOWN" if post_tool else "FAILED")
            if mode != "success":
                assert result["error_code"] == "TOOL_CLEANUP_FAILED"
                assert result["draft_text"] is None
            assert len(result["operation_refs"]) == int(post_tool)
            if post_tool:
                assert result["operation_refs"][0]["outcome"] == "SUCCEEDED"
        assert len(server.calls) == server.target and tools.calls == tools.init_calls == []
        if post_tool:
            assert len(provider.commands) == 1
        assert len(model.chat.completions.calls) == int(family == "kb" and mode == "success")
        assert fault.resources and fault.resources[0].queued > 0
        # Physical resource and actual DB assertions PRECEDE peer drain/fixture
        # cleanup. CLOSED flags and pool bookkeeping are not release evidence.
        assert all(resource.socket.fileno() == -1 for resource in fault.resources)
        assert all(resource.transport.get_write_buffer_size() == 0 for resource in fault.resources)
        assert db_probe.sessions and all(not s.in_transaction() for s in db_probe.sessions)
        assert not db_probe.checkouts and all(driver.is_closed() for driver in db_probe.drivers)
        owners = set(fault.events + fault.scope_events)
        assert len(owners) == 1
        assert all(task.get_name() == "managed-turn-owner" and task.done() for task in owners)
        assert not [t for t in asyncio.all_tasks() - before if t is not asyncio.current_task()
                    and t not in server.tasks and not t.done()]
        if mode == "error":
            assert fault.checkpoint_events
        if mode == "deadline":
            assert fault.scope_events
        server.release.set()
        for peer in server.peers:
            if not peer.writer.transport.is_closing():
                peer.writer.transport.resume_reading()
        for peer in server.peers:
            await asyncio.wait_for(peer.eof.wait(), .5)
        await asyncio.gather(*server.tasks)


@pytest.mark.parametrize("interrupted", [False, True])
def test_native_uvloop_transport_ownership_does_not_require_asyncio_inheritance(monkeypatch, interrupted):
    """Uvicorn's existing standard extra selects this actual native loop."""
    import uvloop

    async def run():
        done = asyncio.Event()
        tasks, acquired, peers = [], [], []
        from httpcore._backends.anyio import AnyIOBackend
        connect = AnyIOBackend.connect_tcp
        async def observe(backend, *args, **kwargs):
            stream = await connect(backend, *args, **kwargs)
            acquired.append(stream._stream)
            return stream
        monkeypatch.setattr(AnyIOBackend, "connect_tcp", observe)
        async def peer(reader, writer):
            peers.append(writer)
            tasks.append(asyncio.current_task())
            writer.transport.pause_reading()
            try:
                await done.wait()
                writer.transport.resume_reading()
                while await reader.read(65536):
                    pass
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(peer, "127.0.0.1", 0, limit=4096)
        server.sockets[0].setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client = cleanup.own_managed_http(httpx.AsyncClient(trust_env=False))
        stream = None
        try:
            stream = await client._transport._pool._network_backend.connect_tcp(
                "127.0.0.1", server.sockets[0].getsockname()[1], timeout=1)
            native = stream._stream
            assert isinstance(native, native_asyncio.SocketStream)
            assert type(native._transport).__module__ == "uvloop.loop"
            assert not isinstance(native._transport, asyncio.Transport)
            sock = native._transport.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            native._transport.write(b"x" * 131072)
            assert native._transport.get_write_buffer_size() > 0
            if interrupted:
                monkeypatch.setattr(cleanup, "anyio", SimpleNamespace(
                    CancelScope=lambda **kwargs: anyio.CancelScope(shield=kwargs.get("shield", False),
                                                                  deadline=anyio.current_time()),
                    current_time=anyio.current_time))
                with pytest.raises(cleanup.ManagedExecutionError, match="TOOL_CLEANUP_FAILED"):
                    await stream.aclose()
                monkeypatch.setattr(cleanup, "anyio", anyio)
                assert native._transport.is_closing()
            if interrupted:
                with pytest.raises(cleanup.ManagedExecutionError, match="TOOL_CLEANUP_FAILED"):
                    await cleanup.close_managed_helper(client)
            else:
                await cleanup.close_managed_helper(client)
            # Physical assertions precede fixture abort or peer drain.
            assert sock.fileno() == -1 and native._transport.get_write_buffer_size() == 0
        finally:
            for native in acquired:
                native._transport.abort()
            done.set()
            for writer in peers:
                if not writer.transport.is_closing():
                    writer.transport.resume_reading()
            await asyncio.gather(*tasks)
            server.close()
            await server.wait_closed()
    uvloop.run(run())
