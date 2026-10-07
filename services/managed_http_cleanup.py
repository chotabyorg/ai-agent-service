"""Same-owner teardown for managed HTTP/1 clients, including OpenAI's pool.

HTTPX 0.28.1 marks the client closed before closing its transport; httpcore
1.0.9 removes pool connections before closing their streams. Retain the actual
socket owners before either operation. These inspected private seams require
the matching requirements pins, and are confined to this module.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from anyio._backends._asyncio import SocketStream, UNIXSocketStream
from importlib.metadata import version
from httpcore._backends.auto import AutoBackend
from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream
from httpcore._synchronization import current_async_library

from services.managed_tools import ManagedExecutionError


class _OwnedStream(AsyncNetworkStream):
    """Keep the acquired native socket even before TLS/protocol installation."""

    def __init__(self, stream, owner):
        if not isinstance(stream, AnyIOStream):
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
        self.native, self.owner = stream, owner
        self._stream = stream._stream
        owner.retain(self._stream)
        self._native_close = stream.aclose
        # Native start_tls's exception cleanup invokes its own aclose. Preserve
        # that path under the same detection policy, including pre-protocol TLS.
        stream.aclose = self.aclose

    async def read(self, max_bytes, timeout=None):
        return await self.native.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self.native.write(buffer, timeout)

    async def aclose(self):
        failed = False
        with anyio.CancelScope(shield=True, deadline=anyio.current_time() + 2) as scope:
            try:
                await self._native_close()
            except BaseException:
                failed = True
        if failed or scope.cancel_called:
            self.owner.failed = True
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED") from None

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # The raw stream is already retained if native start_tls is cancelled
        # before returning a TLS wrapper or installing HTTPcore's connection.
        stream = await self.native.start_tls(ssl_context, server_hostname, timeout)
        return _OwnedStream(stream, self.owner)

    def get_extra_info(self, info):
        return self.native.get_extra_info(info)


class _OwnedBackend(AsyncNetworkBackend):
    def __init__(self, backend, owner):
        if type(backend) not in {AutoBackend, AnyIOBackend}:
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
        self.native, self.owner = backend, owner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self.native.connect_tcp(host, port, timeout, local_address, socket_options)
        # No await between the native acquisition and retention.
        return _OwnedStream(stream, self.owner)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        stream = await self.native.connect_unix_socket(path, timeout, socket_options)
        return _OwnedStream(stream, self.owner)

    async def sleep(self, seconds):
        await self.native.sleep(seconds)


@dataclass(frozen=True)
class _NativeSocket:
    close: Callable[[], Awaitable[None]]
    abort: Callable[[], None]
    socket: Any
    buffer_size: Callable[[], int]


class _HTTPResourceOwner:
    def __init__(self):
        self.sockets = {}
        self.failed = False

    def retain(self, stream):
        while hasattr(stream, "transport_stream"):
            stream = stream.transport_stream
        if isinstance(stream, SocketStream):
            # Native AnyIO owns the actual loop transport. Uvicorn's existing
            # uvloop TCPTransport implements that protocol without inheriting
            # asyncio.Transport; the pinned native stream/backend is the guard.
            transport = stream._transport
            socket = transport.get_extra_info("socket")
            abort = transport.abort
            buffer_size = transport.get_write_buffer_size
        elif isinstance(stream, UNIXSocketStream):
            # Pinned native Unix streams close the captured socket synchronously;
            # they do not use asyncio's graceful buffered transport close.
            socket = stream._raw_socket
            abort = socket.close
            buffer_size = lambda: 0
        else:
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
        if socket is None:
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
        # Capture the actual native owner, never a reusable descriptor number.
        self.sockets.setdefault(id(stream), _NativeSocket(stream.aclose, abort, socket, buffer_size))

    async def aclose(self):
        # Final join runs in the retained task after operation scopes unwind.
        with anyio.CancelScope(shield=True):
            for resource in reversed(list(self.sockets.values())):
                try:
                    await resource.close()
                except (Exception, asyncio.CancelledError):
                    self.failed = True
                finally:
                    # Native aclose can skip abort on an already-closing stream
                    # after its own error/deadline at the graceful checkpoint.
                    # Always finish and join the captured actual physical owner.
                    resource.abort()
                    # uvloop can close its socket one callback before discarding
                    # the native pending write buffer. Join both actual states.
                    while resource.socket.fileno() != -1 or resource.buffer_size() != 0:
                        await anyio.sleep(0)


def own_managed_http(client):
    """Install request-local acquisition ownership before the first HTTP await.

    Supported native ABI is the pinned HTTPX/httpcore AnyIO HTTP/1 transport,
    including default forward/CONNECT proxy mounts. MockTransport is a socketless
    test seam. Other transports/backends are deliberately unsupported.
    """
    if hasattr(client, "_managed_http_owner"):
        return client
    if (current_async_library() != "asyncio" or any(version(package) != expected for package, expected in
            {"httpx": "0.28.1", "httpcore": "1.0.9", "anyio": "4.12.1"}.items())):
        raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
    owner = _HTTPResourceOwner()
    transports = {id(t): t for t in [client._transport, *client._mounts.values()] if t is not None}
    for transport in transports.values():
        if isinstance(transport, httpx.MockTransport):
            continue
        if not isinstance(transport, httpx.AsyncHTTPTransport):
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
        pool = transport._pool
        if pool._http2:
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
        pool._network_backend = _OwnedBackend(pool._network_backend, owner)
    client._managed_http_owner = owner
    return client


async def close_managed_helper(client: httpx.AsyncClient, *, initial_close=None) -> None:
    owner = getattr(client, "_managed_http_owner", None) or _HTTPResourceOwner()
    transports = [client._transport, *client._mounts.values()]
    transports = list({id(value): value for value in transports if value is not None}.values())
    pools, connection_closers, other_transports = [], [], []
    for transport in transports:
        if not isinstance(transport, httpx.AsyncHTTPTransport):
            # In-process MockTransport tests have no sockets/pool.
            other_transports.append(transport)
            continue
        pool = transport._pool
        pools.append(pool)
        for connection in pool.connections:
            connection_closers.append(connection.aclose)
            protocol = connection
            # Native HTTP/1 forwarding/tunnel proxy connections can own another
            # AsyncHTTPConnection before the protocol's actual network stream.
            while getattr(protocol, "_connection", None) is not None:
                protocol = protocol._connection
            if not hasattr(protocol, "_network_stream"):
                continue
            stream = protocol._network_stream._stream
            owner.retain(stream)

    failed = owner.failed
    with anyio.CancelScope(shield=True, deadline=anyio.current_time() + 2) as scope:
        try:
            await (initial_close or client.aclose)()
        except BaseException:
            failed = True
    failed = failed or scope.cancel_called or owner.failed
    # No client.aclose retry: its CLOSED flag may make it a no-op. Finish the
    # retained actual socket/pool ownership in this same task, even if pool
    # bookkeeping was already emptied. Never repeat an HTTP/model/tool request.
    # Like the SDK's final reap/join, an uncooperative final socket close cannot
    # have an absolute OS-independent deadline; we retain/join rather than detach.
    with anyio.CancelScope(shield=True):
        # Always join acquisition owners. A cancelled native TLS handshake can
        # leave no protocol/pool connection at all, even with successful aclose.
        await owner.aclose()
        for close in connection_closers:
            try:
                await close()
            except Exception:
                pass
        # The live sockets are now joined even when pool bookkeeping itself
        # raised after removing them. Finish every remaining independent pool.
        for pool in pools:
            try:
                await pool.aclose()
            except Exception:
                pass
        for transport in other_transports:
            try:
                await transport.aclose()
            except Exception:
                pass
    # Recovery releases resources, but does not turn failed finite close into
    # a successful execution or permit processing another model/tool response.
    if failed or owner.failed:
        raise ManagedExecutionError("TOOL_CLEANUP_FAILED") from None
