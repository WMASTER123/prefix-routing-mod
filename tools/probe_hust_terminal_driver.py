"""CPU HTTP qualification of Rust driver against the actual observer state.

Core utility IPC is a substitute; no HUST engine, NPU or readiness qualification.
The loopback fixture invokes the same FrontendEnds.read/retire used by the hook.
"""

import argparse
import asyncio
import contextlib
import json
import os
import queue
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from prefix_routing_mod.terminal_transport import CORE_METHOD, CoreEnds, FrontendEnds


class _Core:
    def __init__(self):
        self.source = CoreEnds()
        # Explicit substitute identity: no actual engine process exists here.
        self.source.pid = os.getpid() + 1

    async def call_utility_async(self, method, epoch, after, acknowledge):
        if method != CORE_METHOD:
            raise ValueError("Unexpected core fixture method")
        reply = self.source.poll(epoch, after, acknowledge)
        return dict(reply, cleanup_observer_installed=True)


class _Source:
    def __init__(self, scenario, store):
        self.core = _Core()
        self.state = FrontendEnds(
            self.core, "replica", self.core.source.poll(None, 0, 0)
        )
        self.state.ownership_mode = (
            "observe_only" if scenario == "observe_only" else "exclusive_router_v1"
        )
        self.scenario, self.store = scenario, store
        self.token = secrets.token_hex(24)
        self.dropped = self.retired = self.calls = self.ack_new_records = 0
        self.injected = False
        self.errors = []
        self.children = []
        source = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.handle_rpc(False)

            def do_POST(self):
                if self.headers.get("Content-Length") != "2":
                    self.send_error(400)
                    return
                if self.rfile.read(2) != b"{}":
                    self.send_error(400)
                    return
                self.handle_rpc(True)

            def handle_rpc(self, retire):
                if self.headers.get("X-Prefix-Routing-Control") != source.token:
                    self.send_error(403)
                    return
                parsed = urlsplit(self.path)
                expected = "/prefix-routing/terminal" + ("/retire" if retire else "")
                if parsed.path != expected or len(self.path) > 4096:
                    self.send_error(404)
                    return
                query = {
                    key: values[0] for key, values in parse_qs(parsed.query).items()
                }
                try:
                    source.calls += 1
                    if source.scenario == "unavailable" and not source.injected:
                        source.injected = True
                        self.send_error(503)
                        return
                    if retire:
                        through = int(query["through"])
                        source.check_commit(through)
                        reply = source.loop.run_until_complete(
                            source.state.retire(
                                query["observer_epoch"],
                                through,
                                int(query["checkpointed_through"]),
                            )
                        )
                        source.retired += reply["retired"]
                        lose = source.scenario == "retire_loss" and through == 1
                    else:
                        after, ack = int(query["after"]), int(query["acknowledge"])
                        source.check_commit(ack)
                        if source.scenario == "concurrent_ack" and ack == 1:
                            source.core.source.observe(
                                source.children[1], "FINISHED_ABORTED"
                            )
                        reply = source.loop.run_until_complete(
                            source.state.read(query["observer_epoch"], after, ack)
                        )
                        if ack == 1 and reply["records"]:
                            source.ack_new_records += 1
                        if source.scenario == "identity_change" and ack == 1:
                            reply["frontend_pid"] += 1
                        lose = source.scenario == "ack_loss" and ack == 1
                    if lose and not source.injected:
                        source.injected = True
                        source.dropped += 1
                        self.close_connection = True
                        with contextlib.suppress(OSError):
                            self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    payload = json.dumps(reply).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as error:
                    source.errors.append(type(error).__name__)
                    self.send_error(409)

            def log_message(self, *_):
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.1
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def check_commit(self, through):
        # Independent cross-process read: never ACK/retire beyond durable state.
        with sqlite3.connect(self.store) as connection:
            payload = connection.execute(
                "SELECT payload FROM runtime_items WHERE key='c:replica'"
            ).fetchone()[0]
        if through > json.loads(payload)["through"]:
            raise AssertionError("Remote confirmation preceded durable commit")

    def serve(self):
        self.loop = asyncio.new_event_loop()
        try:
            while not self.stop.is_set():
                self.server.handle_request()
        finally:
            self.loop.close()

    def register(self, attempt, *, ended=True):
        child = f"child-{len(self.children)}"
        self.children.append(child)
        self.state.journal.begin_managed(attempt, 1)
        self.state.journal.bind_child(attempt, child)
        self.state.journal.seal(attempt)
        if ended:
            self.core.source.observe(child, "FINISHED_STOPPED")

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.server.server_close()
        if self.thread.is_alive():
            raise RuntimeError("Source fixture did not stop")


def _receive(lines):
    value = lines.get(timeout=10)
    if value is None:
        raise RuntimeError("Driver fixture exited before a response")
    return json.loads(value)


def qualify(binary, scenario, *, background=False):
    with tempfile.TemporaryDirectory(prefix="prefix-hust-driver-") as directory:
        root = Path(directory)
        source = _Source(scenario, root / "runtime.sqlite")
        config = {
            "cursor": {
                "source_id": "replica",
                "worker_url": source.origin,
                "instance_epoch": source.state.journal.binding.instance_epoch,
                "observer_epoch": source.state.journal.binding.observer_epoch,
                "through": 0,
            },
            "token": source.token,
            "frontend_pid": os.getpid(),
            "core_pid": source.core.source.pid,
            "ownership": source.state.ownership_mode,
            "store": str(source.store),
        }
        path = root / "private-config.json"
        with os.fdopen(
            os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
        ) as stream:
            json.dump(config, stream)
        process = subprocess.Popen(
            [str(binary), str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        lines = queue.Queue(maxsize=64)

        def reader():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        output_thread = threading.Thread(target=reader, daemon=True)
        output_thread.start()
        source.thread.start()
        try:
            assert _receive(lines) == {"ready": True, "npu_validated": False}

            def command(value):
                process.stdin.write(value + "\n")
                process.stdin.flush()
                return _receive(lines)

            first = command("reserve")
            source.register(first["result"]["attempt"])
            if scenario == "concurrent_ack":
                second = command("reserve")
                source.register(second["result"]["attempt"], ended=False)
            if scenario == "unknown_attempt":
                source.register("prm-unknown")
            first_step = command("run" if background else "step")
            if scenario == "unknown_attempt":
                assert first_step["result"]["step"] == "fatal"
                assert first_step["active"] == 1 and first_step["stored_through"] == 0
                assert source.state.journal.acknowledged == 0
            elif scenario == "identity_change":
                assert first_step["result"]["step"] == "fatal"
                assert first_step["active"] == 0 and first_step["stored_through"] == 1
            else:
                if not background and scenario in {
                    "ack_loss",
                    "retire_loss",
                    "unavailable",
                }:
                    assert first_step["result"]["step"] == "retry"
                    assert first_step["active"] == (
                        1 if scenario == "unavailable" else 0
                    )
                if not background and scenario == "concurrent_ack":
                    assert (
                        first_step["active"] == 1 and first_step["stored_through"] == 1
                    )
                    assert source.ack_new_records == 1
                last = first_step if background else command("step")
                assert (
                    last["active"] == 0
                    and not last["pending"]
                    and last["failed"] == background
                )
                assert last["committed_statuses"].get("FINISHED_STOPPED") == 1
                if background:
                    assert last["result"]["step"] == "stopped"
                if scenario == "concurrent_ack":
                    assert last["committed_statuses"].get("FINISHED_ABORTED") == 1
                assert last["stored_through"] == (
                    2 if scenario == "concurrent_ack" else 1
                )
                if scenario in {"ack_loss", "retire_loss"}:
                    assert source.dropped == 1
                    assert first_step["generation"] == last["generation"]
                if scenario == "observe_only":
                    assert (
                        source.retired == 0 and len(source.state.journal.families) == 1
                    )
                else:
                    assert source.retired == last["stored_through"]
                    assert not source.state.journal.families
            assert not source.errors
            process.stdin.close()
            assert process.wait(timeout=5) == 0, process.stderr.read(4096)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            source.close()
            output_thread.join(timeout=3)
            process.stdout.close()
            process.stderr.close()
        return {
            "scenario": scenario,
            "background": background,
            "status": "pass",
            "calls": source.calls,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    results = [
        qualify(binary, scenario)
        for scenario in (
            "normal",
            "concurrent_ack",
            "ack_loss",
            "retire_loss",
            "unavailable",
            "observe_only",
            "identity_change",
            "unknown_attempt",
        )
    ]
    results.extend(
        qualify(binary, scenario, background=True)
        for scenario in (
            "normal",
            "concurrent_ack",
            "ack_loss",
            "retire_loss",
            "unavailable",
            "observe_only",
            "identity_change",
            "unknown_attempt",
        )
    )
    print(
        json.dumps(
            {
                "status": "pass",
                "scope": "CPU-real-observer-HTTP",
                "core_utility": "substitute",
                "npu_validated": False,
                "cases": results,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
