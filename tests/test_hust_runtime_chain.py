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
            # Closing a Windows socket with an unread request body can reset
            # the connection before the client has consumed the 200 response.
            assert self.rfile.read(int(self.headers["Content-Length"])) == b"{}"
            state["calls"] += 1
            body = json.dumps({"retired": 1 if state["calls"] == 1 else 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
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
        assert proxy.dropped == 1, (
            "An upstream failure is not a committed-response loss: "
            f"{proxy.upstream_errors}"
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request(), timeout=2
            ) as response:
                assert json.load(response) == {"retired": 0}
        except urllib.error.HTTPError:
            pytest.fail(f"Unexpected upstream failure: {proxy.upstream_errors}")
        assert state["calls"] == proxy.forwarded == 2
        assert proxy.dropped == 1
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def test_control_modes_reject_invalid_combinations_before_launch(monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid mode must fail before launching services")

    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", unexpected)
    with pytest.raises(ValueError, match="Control qualification requires"):
        hust_runtime_chain.run(
            SimpleNamespace(exercise_control_gate=True), tmp_path, {}
        )


def test_control_fixture_auth_failure_recovery_and_injected_identity():
    from tools.control_fault_proxy import ControlFaultProxy

    token = "c" * 32

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["X-Prefix-Routing-Control"] == token
            body = json.dumps({"binding": {"observer_epoch": "original"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    proxy = ControlFaultProxy(f"http://127.0.0.1:{upstream.server_port}", token)
    proxy.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, method="GET", credential=token):
        return opener.open(
            urllib.request.Request(
                proxy.origin + path,
                data=b"" if method == "POST" else None,
                method=method,
                headers={"X-Prefix-Routing-Control": credential},
            ),
            timeout=3,
        )

    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            call("/__fault/fail", "POST", "wrong")
        assert error.value.code == 404
        for mode in ("pass", "fail", "pass", "identity"):
            with call(f"/__fault/{mode}", "POST") as response:
                assert response.status == 200
            if mode == "fail":
                with pytest.raises(urllib.error.HTTPError) as error:
                    call("/prefix-routing/identity")
                assert error.value.code == 503
            else:
                with call("/prefix-routing/identity") as response:
                    epoch = json.load(response)["binding"]["observer_epoch"]
                    assert epoch == (
                        "original-fault" if mode == "identity" else "original"
                    )
        assert proxy.failures == proxy.conflicts == 1
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
