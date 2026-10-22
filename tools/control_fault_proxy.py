"""Owned loopback identity fault fixture; never part of the serving path."""

import json
import secrets
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class ControlFaultProxy:
    def __init__(self, target: str, token: str):
        parsed = urlsplit(target)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not parsed.port
            or len(token) < 32
        ):
            raise ValueError("Control fixture requires an explicit loopback origin")
        self._mode = "pass"
        self._lock = threading.Lock()
        self.failures = 0
        self.conflicts = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def authorized(self):
                return secrets.compare_digest(
                    self.headers.get("X-Prefix-Routing-Control", ""), token
                )

            def reply(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                mode = self.path.removeprefix("/__fault/")
                if (
                    not self.authorized()
                    or self.path != f"/__fault/{mode}"
                    or mode not in {"pass", "fail", "identity"}
                    or self.headers.get("Content-Length", "0") != "0"
                ):
                    self.reply(404, b"{}")
                    return
                with owner._lock:
                    owner._mode = mode
                self.reply(200, b"{}")

            def do_GET(self):
                if self.path != "/prefix-routing/identity" or not self.authorized():
                    self.reply(404, b"{}")
                    return
                with owner._lock:
                    mode = owner._mode
                    if mode == "fail":
                        owner.failures += 1
                if mode == "fail":
                    self.reply(503, b"{}")
                    return
                request = urllib.request.Request(
                    target + self.path,
                    headers={"X-Prefix-Routing-Control": token},
                )
                try:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    )
                    with opener.open(request, timeout=3) as response:
                        payload = response.read(4097)
                        if len(payload) > 4096:
                            raise ValueError("Identity reply exceeds fixture budget")
                    if mode == "identity":
                        value = json.loads(payload)
                        value["binding"]["observer_epoch"] += "-fault"
                        payload = json.dumps(value).encode()
                        if len(payload) > 4096:
                            raise ValueError("Identity reply exceeds fixture budget")
                        with owner._lock:
                            owner.conflicts += 1
                except Exception:
                    self.reply(502, b"{}")
                    return
                self.reply(200, payload)

            def log_message(self, *_):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Control fixture did not stop")
