"""Tiny loopback fixture, not a production inference server or proxy."""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404, b"{}")

    def do_POST(self):
        if self.path not in {"/v1/chat/completions", "/v1/completions"}:
            self.reply(404, b"{}")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 65536:
                self.reply(413, b"{}")
                return
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError("body must be object")
        except (ValueError, UnicodeError):
            self.reply(400, b"{}")
            return
        if request.get("model") == "fixture-error":
            self.reply(503, b'{"error":{"message":"synthetic unavailable"}}')
        elif request.get("stream"):
            self.reply(
                200,
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
                b"data: [DONE]\n\n",
                "text/event-stream",
            )
        else:
            self.reply(200, b'{"choices":[{"message":{"content":"hello world"}}]}')


@contextmanager
def mock_backend(handler=Handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("fixture server failed to terminate")
