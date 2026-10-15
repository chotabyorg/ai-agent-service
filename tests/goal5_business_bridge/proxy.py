"""Owned HTTP forwarding proxies with deterministic pre/post-commit barriers."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


class ForwardProxy:
    def __init__(self, listener, upstream: str, name: str):
        self.upstream = upstream.rstrip("/")
        self.name = name
        self.captured = []
        self._lock = threading.Lock()
        self._barrier = None
        self._detach_next = None
        self.detached_workers = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def handle_forward(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 65536:
                    self.send_error(413)
                    return
                body = self.rfile.read(length) if length else b""
                received_at_ns = time.monotonic_ns()
                headers = {
                    key: value for key, value in self.headers.items()
                    if key.lower() in {"authorization", "x-service-token", "content-type", "accept"}
                }
                record = {"method": self.command, "path": self.path,
                          "body": body, "headers": dict(headers),
                          "received_at_monotonic_ns": received_at_ns}
                with owner._lock:
                    owner.captured.append(record)
                    barrier = owner._barrier
                    detached = owner._detach_next
                    if detached and self.path == detached["path"]:
                        owner._detach_next = None
                    else:
                        detached = None
                    if barrier and self.path == barrier["path"]:
                        owner._barrier = None
                    else:
                        barrier = None

                def record_response(status, response_body):
                    with owner._lock:
                        record["response_status"] = status
                        record["response_body"] = response_body
                        record["upstream_response_at_monotonic_ns"] = time.monotonic_ns()

                if detached:
                    def continue_upstream():
                        try:
                            with httpx.Client(timeout=35, trust_env=False,
                                              follow_redirects=False) as client:
                                response = client.request(
                                    self.command, owner.upstream + self.path,
                                    content=body, headers=headers,
                                )
                                record_response(response.status_code, response.content)
                                detached["status"] = response.status_code
                                detached["body"] = response.text[:500]
                        except httpx.HTTPError:
                            record_response(None, b"")
                            detached["status"] = None
                        finally:
                            detached["done"].set()

                    worker = threading.Thread(target=continue_upstream, daemon=True,
                                              name=f"goal5-{owner.name}-detached-request")
                    owner.detached_workers.append(worker)
                    worker.start()
                    detached["started"].set()
                    if not detached["caller_release"].wait(35):
                        payload = b'{"detail":"Goal5 detached caller release timeout"}'
                        self.send_response(504)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    payload = b'{"detail":"Goal5 test detached caller"}'
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if barrier and barrier["stage"] == "before":
                    barrier["entered"].set()
                    if not barrier["release"].wait(30):
                        self.send_error(504)
                        return
                try:
                    with httpx.Client(timeout=35, trust_env=False, follow_redirects=False) as client:
                        response = client.request(
                            self.command, owner.upstream + self.path,
                            content=body, headers=headers,
                        )
                except httpx.HTTPError:
                    record_response(502, b"")
                    self.send_error(502)
                    return
                record_response(response.status_code, response.content)
                if barrier and barrier["stage"] == "after" and response.status_code == 200:
                    barrier["entered"].set()
                    if not barrier["release"].wait(30):
                        self.send_error(504)
                        return
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in {"content-length", "transfer-encoding", "connection", "date", "server"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                try:
                    self.wfile.write(response.content)
                    with owner._lock:
                        record["response_sent_at_monotonic_ns"] = time.monotonic_ns()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            do_GET = do_POST = do_PUT = do_DELETE = handle_forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.socket.close()
        self.server.socket = listener
        self.server.server_address = listener.getsockname()
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name=f"goal5-{name}-proxy")

    @property
    def port(self):
        return int(self.server.server_address[1])

    def start(self):
        self.thread.start()

    def arm(self, stage: str, path: str = "/internal/v1/external-effect-permits"):
        if stage not in {"before", "after"}:
            raise ValueError("Unknown proxy barrier stage")
        event = threading.Event()
        release = threading.Event()
        with self._lock:
            self._barrier = {"stage": stage, "path": path,
                             "entered": event, "release": release}
        return event, release

    def detach_next(self, path: str):
        state = {"path": path, "started": threading.Event(),
                 "done": threading.Event(), "caller_release": threading.Event(),
                 "status": None}
        with self._lock:
            if self._detach_next is not None:
                raise AssertionError("A detached test request is already armed")
            self._detach_next = state
        return state

    def clear_capture(self):
        with self._lock:
            self.captured.clear()

    def snapshot(self):
        with self._lock:
            return list(self.captured)

    def close(self):
        with self._lock:
            barrier = self._barrier
            self._barrier = None
        if barrier:
            barrier["release"].set()
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        for worker in self.detached_workers:
            worker.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("Owned Goal5 proxy did not stop")
