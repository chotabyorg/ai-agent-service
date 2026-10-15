"""Exact loopback-only networking guard for the opt-in paired Goal5 suite."""

import ipaddress
import socket

PREIMPORT_GUARD = None


def install_guard(endpoints, *, allow_bound_loopback=False):
    if type(allow_bound_loopback) is not bool:
        raise RuntimeError("Invalid Goal5 dynamic-loopback option")
    class Guard(set):
        pass

    allowed = Guard((str(host), int(port)) for host, port in endpoints)
    if not allowed or any(
        host != "127.0.0.1" or not 1 <= port <= 65535 for host, port in allowed
    ):
        raise RuntimeError("Invalid Goal5 loopback allowlist")
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo
    original_bind = socket.socket.bind
    bound_loopback = set()
    allowed.bound_loopback = bound_loopback

    def bind(sock, address):
        result = original_bind(sock, address)
        if allow_bound_loopback and sock.family in {socket.AF_INET, socket.AF_INET6}:
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    bound = sock.getsockname()
                    bound_loopback.add((str(bound[0]), int(bound[1])))
            except (IndexError, TypeError, ValueError):
                pass
        return result

    def check(address):
        if isinstance(address, tuple) and tuple(address[:2]) in allowed:
            return
        if allow_bound_loopback and isinstance(address, tuple) and len(address) >= 2:
            host, port = address[:2]
            if host == "localhost" and any(bound_port == port
                                            for _, bound_port in bound_loopback):
                return
            try:
                endpoint = (str(ipaddress.ip_address(host)), int(port))
            except (TypeError, ValueError):
                endpoint = None
            if endpoint in bound_loopback:
                return
        raise RuntimeError("Goal5 network destination forbidden")

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        elif sock.family == socket.AF_UNIX and address:
            raise RuntimeError("Goal5 named Unix socket forbidden")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        elif sock.family == socket.AF_UNIX and address:
            raise RuntimeError("Goal5 named Unix socket forbidden")
        return original_connect_ex(sock, address)

    def resolve(host, port, *args, **kwargs):
        check((host, port))
        values = original_resolve(host, port, *args, **kwargs)
        if any(not ipaddress.ip_address(item[4][0]).is_loopback for item in values):
            raise RuntimeError("Goal5 DNS resolution escaped loopback")
        return values

    socket.socket.bind = bind
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = resolve
    return allowed


def self_check():
    for method in ("connect", "connect_ex"):
        with socket.socket() as sock:
            try:
                getattr(sock, method)(("203.0.113.1", 443))
            except RuntimeError:
                continue
            raise RuntimeError("Goal5 network guard self-check failed")
    try:
        socket.getaddrinfo("goal5-external-blocked.invalid", 443)
    except RuntimeError:
        return 3
    raise RuntimeError("Goal5 DNS guard self-check failed")
