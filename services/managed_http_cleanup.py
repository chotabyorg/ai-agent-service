"""Same-owner teardown for managed HTTP/1 clients, including OpenAI's pool.

HTTPX 0.28.1 marks the client closed before closing its transport; httpcore
1.0.9 removes pool connections before closing their streams. Retain the actual
socket owners before either operation. These inspected private seams require
the matching requirements pins, and are confined to this module.
"""

import anyio
import httpx

from services.managed_tools import ManagedExecutionError


async def close_managed_helper(client: httpx.AsyncClient, *, initial_close=None) -> None:
    transports = [client._transport, *client._mounts.values()]
    transports = list({id(value): value for value in transports if value is not None}.values())
    pools, socket_closers, connection_closers, other_transports = [], [], [], []
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
            # On failed graceful TLS close, join the underlying socket owner.
            while hasattr(stream, "transport_stream"):
                stream = stream.transport_stream
            socket_closers.append(stream.aclose)

    failed = False
    with anyio.CancelScope(shield=True, deadline=anyio.current_time() + 2) as scope:
        try:
            await (initial_close or client.aclose)()
        except BaseException:
            failed = True
    failed = failed or scope.cancel_called
    if not failed:
        return

    # No client.aclose retry: its CLOSED flag may make it a no-op. Finish the
    # retained actual socket/pool ownership in this same task, even if pool
    # bookkeeping was already emptied. Never repeat an HTTP/model/tool request.
    # Like the SDK's final reap/join, an uncooperative final socket close cannot
    # have an absolute OS-independent deadline; we retain/join rather than detach.
    with anyio.CancelScope(shield=True):
        for close in socket_closers:
            await close()
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
    raise ManagedExecutionError("TOOL_CLEANUP_FAILED") from None
