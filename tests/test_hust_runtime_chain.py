import json
import threading
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tools import hust_runtime_chain


def test_stale_wheel_is_rejected_before_any_backend_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", lambda: {})
    monkeypatch.setattr(hust_runtime_chain, "FrontendEnds", object)

    def unexpected(*args, **kwargs):
        pytest.fail("A stale wheel must fail before backend inspection or launch")

    monkeypatch.setattr(hust_runtime_chain, "inspect_backend", unexpected)
    monkeypatch.setattr(hust_runtime_chain, "OwnedBackend", unexpected)
    with pytest.raises(RuntimeError, match="independent identity"):
        hust_runtime_chain.run(SimpleNamespace(), tmp_path, {})


def test_managed_retirement_requires_durable_store_before_backend_launch(
    monkeypatch, tmp_path
):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid managed retirement mode must fail before backend launch")

    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", unexpected)
    monkeypatch.setattr(hust_runtime_chain, "OwnedBackend", unexpected)
    args = SimpleNamespace(managed_terminal_retirement=True, durable_store=False)
    with pytest.raises(ValueError, match="requires --durable-store"):
        hust_runtime_chain.run(args, tmp_path, {})


def test_retire_loss_requires_managed_mode_before_backend_launch(monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid fault mode must fail before backend launch")

    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", unexpected)
    args = SimpleNamespace(
        managed_terminal_retirement=False,
        durable_store=True,
        inject_retire_response_loss=True,
    )
    with pytest.raises(ValueError, match="requires managed retirement"):
        hust_runtime_chain.run(args, tmp_path, {})


def test_router_recovery_rejects_combined_fault_modes_before_backend_launch(
    monkeypatch, tmp_path
):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid recovery mode must fail before backend launch")

    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", unexpected)
    args = SimpleNamespace(
        managed_terminal_retirement=True,
        durable_store=True,
        inject_retire_response_loss=True,
        exercise_router_recovery=True,
        churn_families=0,
        churn_children_per_family=0,
    )
    with pytest.raises(ValueError, match="without other fault cases"):
        hust_runtime_chain.run(args, tmp_path, {})


def test_retire_loss_proxy_drops_only_the_first_committed_reply():
    state = {"calls": 0}

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            state["calls"] += 1
            body = json.dumps({"retired": 1 if state["calls"] == 1 else 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    proxy = hust_runtime_chain._RetireLossProxy(
        f"http://127.0.0.1:{upstream.server_port}", opener
    )
    proxy.start()

    def request():
        return urllib.request.Request(
            proxy.origin + "/prefix-routing/terminal/retire?observer_epoch=e"
            "&through=1&checkpointed_through=1",
            data=b"{}",
            method="POST",
            headers={"X-Prefix-Routing-Control": "x" * 32},
        )

    try:
        with pytest.raises((RemoteDisconnected, urllib.error.URLError)):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request(), timeout=2
            )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request(), timeout=2
        ) as response:
            assert json.load(response) == {"retired": 0}
        assert state["calls"] == proxy.forwarded == 2
        assert proxy.dropped == 1
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
