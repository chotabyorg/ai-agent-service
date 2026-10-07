"""Exact owned-loopback guard installed before runtime/client imports."""

import socket

PREIMPORT_GUARD = None


def install_guard(endpoints):
    class Guard(set):
        def restore(self):
            socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, resolve

    allowed = Guard((str(host), int(port)) for host, port in endpoints)
    if not allowed or any(host != "127.0.0.1" or not 1 <= port <= 65535
                          for host, port in allowed):
        raise RuntimeError("Invalid Goal3 endpoint allowlist")
    connect, connect_ex, resolve = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def check(address):
        if not isinstance(address, tuple) or address[:2] not in allowed:
            raise RuntimeError("Goal3 network destination forbidden")

    def guarded_connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        elif sock.family == socket.AF_UNIX and address:
            raise RuntimeError("Goal3 named Unix socket forbidden")
        return connect(sock, address)

    def guarded_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        elif sock.family == socket.AF_UNIX and address:
            raise RuntimeError("Goal3 named Unix socket forbidden")
        return connect_ex(sock, address)

    def guarded_resolve(host, port, *args, **kwargs):
        check((host, port))
        return resolve(host, port, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = (
        guarded_connect, guarded_ex, guarded_resolve)
    return allowed


def self_check():
    for method in ("connect", "connect_ex"):
        with socket.socket() as sock:
            try:
                getattr(sock, method)(("203.0.113.1", 443))
            except RuntimeError:
                pass
            else:
                raise RuntimeError("Goal3 network self-check failed")
    try:
        socket.getaddrinfo("not-an-owned-endpoint.invalid", 443)
    except RuntimeError:
        return 3
    raise RuntimeError("Goal3 DNS self-check failed")
