"""Finite real public HTTP chain qualification on two owned HUST TP1 services.

Launch, exact profile checks and cleanup reuse existing qualification utilities.
The Rust binary owns M1/M2/M3/M4/M5/M7; Python is not an inference proxy.
"""

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prefix_routing_mod.adapters.profiles import inspect_backend
from prefix_routing_mod.terminal_transport import FrontendEnds
from tools.control_fault_proxy import ControlFaultProxy
from tools.hust_native_probe import (
    PROFILE,
    command,
    require_terminal_plugins,
    service_environment,
)
from tools.live_terminal_cases import MODEL
from tools.regress_kv_recovery import OwnedBackend
from tools.restore_runtime import private_path
from tools.smoke_backend import ipc_endpoint, stop_owned_group


class _RetireLossProxy:
    """Loopback-only qualification proxy that drops one committed reply."""

    def __init__(self, target, opener):
        self.target = target
        # Validate the caller's explicit no-proxy client shape, but do not
        # share one mutable urllib opener across the harness and server thread.
        if opener is None:
            raise ValueError("A no-proxy opener is required")
        self.dropped = 0
        self.forwarded = 0
        self.upstream_errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if (
                    not self.path.startswith("/prefix-routing/terminal/retire?")
                    or len(self.path.encode()) > 4096
                    or self.headers.get("Content-Length") not in (None, "0", "2")
                ):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                request = urllib.request.Request(
                    owner.target + self.path,
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": self.headers.get(
                            "Content-Type", "application/json"
                        ),
                        "X-Prefix-Routing-Control": self.headers.get(
                            "X-Prefix-Routing-Control", ""
                        ),
                    },
                )
                try:
                    upstream = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    )
                    with upstream.open(request, timeout=5) as response:
                        payload = response.read(4097)
                        if len(payload) > 4096 or response.status != 200:
                            raise RuntimeError(
                                "Unexpected retirement upstream response"
                            )
                        content_type = response.headers.get(
                            "Content-Type", "application/json"
                        )
                except Exception as error:
                    if len(owner.upstream_errors) < 4:
                        owner.upstream_errors.append(type(error).__name__)
                    self.send_error(502)
                    return
                owner.forwarded += 1
                if owner.dropped == 0:
                    owner.dropped = 1
                    self.close_connection = True
                    with contextlib.suppress(OSError):
                        self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="retire-loss-proxy",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Retirement fault proxy did not stop")


def run(args, output, report):
    managed_retirement = bool(getattr(args, "managed_terminal_retirement", False))
    inject_retire_loss = bool(getattr(args, "inject_retire_response_loss", False))
    if managed_retirement and not getattr(args, "durable_store", False):
        raise ValueError("Managed terminal retirement requires --durable-store")
    if inject_retire_loss and not managed_retirement:
        raise ValueError(
            "Retirement response-loss injection requires managed retirement"
        )
    churn_families = int(getattr(args, "churn_families", 0))
    churn_children = int(getattr(args, "churn_children_per_family", 0))
    exercise_recovery = bool(getattr(args, "exercise_router_recovery", False))
    exercise_control = bool(getattr(args, "exercise_control_gate", False))
    if exercise_control and not (
        managed_retirement
        and getattr(args, "durable_store", False)
        and not exercise_recovery
        and not inject_retire_loss
        and churn_families == 0
    ):
        raise ValueError(
            "Control qualification requires managed durable mode "
            "without other fault cases"
        )
    if churn_families and not (
        managed_retirement and 512 < churn_families <= 1024 and 1 <= churn_children <= 8
    ):
        raise ValueError("Bounded churn requires managed retirement and valid limits")
    if exercise_recovery and not (
        managed_retirement
        and getattr(args, "durable_store", False)
        and not inject_retire_loss
        and churn_families == 0
    ):
        raise ValueError(
            "Router recovery requires managed durable mode without other fault cases"
        )
    report["terminal_entry_points"] = require_terminal_plugins()
    if not hasattr(FrontendEnds, "identity"):
        raise RuntimeError("Installed MOD wheel lacks the independent identity port")
    report["interface"] = inspect_backend(PROFILE)
    candidate = json.loads(
        (
            Path(__file__).resolve().parents[1] / "runtime/candidates/hust-cedb477.json"
        ).read_text("utf-8")
    )
    acl = Path(candidate["cann_acl_path"])
    if (
        hashlib.sha256((acl / "acl.so").read_bytes()).hexdigest()
        != candidate["cann_acl_sha256"]
    ):
        raise ValueError("CANN ACL fingerprint differs")
    assets = {
        name: hashlib.sha256((args.model / name).read_bytes()).hexdigest()
        for name in (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "chat_template.jinja",
        )
        if (args.model / name).is_file()
    }
    report["model_assets_sha256"] = assets
    identity = hashlib.sha256(
        json.dumps(
            {
                "assets": assets,
                "profile": PROFILE,
                "layout": "fp16-full-attention-tp1-dp1-group0-block128-sha256",
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    backends, workers, retirement_proxies, control_proxies = [], [], [], []
    process = None
    try:
        for device in (0, 1):
            root = output / f"replica-{device}"
            root.mkdir()
            origin = f"http://127.0.0.1:{args.port + device}"
            token = secrets.token_hex(32)
            worker_id = f"replica-{device}"
            backend = OwnedBackend(
                command(
                    Path(sys.executable).with_name("vllm"),
                    args.model,
                    args.port + device,
                    root,
                ),
                service_environment(
                    device,
                    root,
                    acl,
                    pycache_root=args.pycache_root,
                    token=token,
                    worker=worker_id,
                    ownership_mode=(
                        "exclusive_router_v1" if managed_retirement else "observe_only"
                    ),
                ),
                root / "backend",
                opener,
                origin,
            )
            backends.append(backend)
            backend.start()
            health_origin = None
            if exercise_control:
                proxy = ControlFaultProxy(origin, token)
                proxy.start()
                control_proxies.append(proxy)
                health_origin = proxy.origin
            retirement_origin = None
            if inject_retire_loss:
                proxy = _RetireLossProxy(origin, opener)
                proxy.start()
                retirement_proxies.append(proxy)
                retirement_origin = proxy.origin
            workers.append(
                {
                    "origin": origin,
                    **({"health_origin": health_origin} if health_origin else {}),
                    **(
                        {"retirement_origin": retirement_origin}
                        if retirement_origin is not None
                        else {}
                    ),
                    "token": token,
                    "worker_id": worker_id,
                    "frontend_pid": backend.process.pid,
                    "live_endpoint": ipc_endpoint(root / "events.sock"),
                    "replay_endpoint": ipc_endpoint(root / "replay.sock"),
                }
            )
        config = {
            "workers": workers,
            "tokenizer": str(args.model / "tokenizer.json"),
            "tokenizer_sha256": assets["tokenizer.json"],
            "identity": identity,
            "model": MODEL,
            "terminal_ownership_mode": (
                "exclusive_router_v1" if managed_retirement else "observe_only"
            ),
            "expected_retire_response_losses": (
                len(workers) if inject_retire_loss else 0
            ),
            "churn_families": churn_families,
            "churn_children_per_family": churn_children,
            "exercise_control_gate": exercise_control,
        }
        if args.durable_store:
            config["durable_store"] = str(output / "runtime.sqlite")
        config_path = output / "private-runtime-config.json"
        with os.fdopen(
            os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            json.dump(config, stream)
        with args.binary.open("rb") as binary:
            report["binary_sha256"] = hashlib.file_digest(binary, "sha256").hexdigest()

        def execute_router(log_name, mode=None, expected=0, timeout=180):
            nonlocal process
            invocation = [str(args.binary), str(config_path)]
            if mode is not None:
                invocation.append(mode)
            path = output / log_name
            with path.open("xb") as log:
                process = subprocess.Popen(
                    invocation,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                process.wait(timeout=timeout)
            if process.returncode != expected:
                raise RuntimeError(
                    "Fixed Router chain exited "
                    f"{process.returncode}, expected {expected}"
                )
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Router evidence log exceeds budget")
            process = None
            return path

        if exercise_recovery:
            crash_log = execute_router(
                "router-crash.log", "crash-after-send", expected=86
            )
            if "intentional_router_crash_after_durable_send" not in crash_log.read_text(
                "utf-8"
            ):
                raise ValueError("Router did not reach the durable sent crash point")
            router_log = execute_router(
                "router-recovery.log", "recover-after-crash", timeout=240
            )
        else:
            router_log = execute_router(
                "router.log",
                timeout=180 if not churn_families else 180 + 3 * churn_families,
            )
        records = []
        for line in router_log.read_text("utf-8").splitlines():
            if line.startswith("{"):
                value = json.loads(line)
                expected_scope = (
                    "router-crash-paused-recovery"
                    if exercise_recovery
                    else "finite-public-http-real-M1-M2-M3-M4-M5-M7"
                )
                if value.get("scope") == expected_scope:
                    records.append(value)
        if len(records) != 1 or records[0].get("status") != "pass":
            raise ValueError("Missing complete public-chain report")
        if exercise_recovery:
            if not all(
                records[0].get(field) is True
                for field in (
                    "paused_admission_rejected",
                    "old_cursor_rejected",
                    "synthetic_replacement_binding_rejected",
                    "post_resume_request_completed",
                    "durable_recovery_verified",
                )
            ):
                raise ValueError("Router recovery coverage differs")
            if records[0].get("m2_recovery_mode") != "unknown_until_fresh_source":
                raise ValueError("Unsafe or unknown M2 recovery mode")

            original = backends[0]
            original.stop()
            if original.cleanup["forced_group_cleanup"]:
                raise RuntimeError(
                    "Original backend required forced replacement cleanup"
                )
            replacement_root = output / "replica-0-replacement"
            replacement_root.mkdir()
            replacement_token = secrets.token_hex(32)
            replacement = OwnedBackend(
                command(
                    Path(sys.executable).with_name("vllm"),
                    args.model,
                    args.port,
                    replacement_root,
                ),
                service_environment(
                    0,
                    replacement_root,
                    acl,
                    pycache_root=args.pycache_root,
                    token=replacement_token,
                    worker="replica-0",
                    ownership_mode="exclusive_router_v1",
                ),
                replacement_root / "backend",
                opener,
                workers[0]["origin"],
            )
            backends.append(replacement)
            replacement.start()
            workers[0].update(
                token=replacement_token,
                frontend_pid=replacement.process.pid,
                live_endpoint=ipc_endpoint(replacement_root / "events.sock"),
                replay_endpoint=ipc_endpoint(replacement_root / "replay.sock"),
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            replacement_log = execute_router(
                "router-replaced-source.log", "reject-replaced-source"
            )
            replacement_records = [
                json.loads(line)
                for line in replacement_log.read_text("utf-8").splitlines()
                if line.startswith("{")
                and json.loads(line).get("scope") == "replaced-terminal-source-rejected"
            ]
            if len(replacement_records) != 1:
                raise ValueError("Actual control instance replacement was not rejected")
            records[0]["actual_source_replacement_verified"] = True
            report["replaced_source"] = replacement_records[0]
            report.update(
                status="pass",
                durable_recovery_verified=True,
                chain=records[0],
            )
            return
        if (
            bool(records[0].get("durable_commit_ack_and_paused_reopen_verified"))
            != args.durable_store
        ):
            raise ValueError("Durable qualification result differs from requested mode")
        if (
            bool(records[0].get("managed_terminal_retirement_verified"))
            != managed_retirement
        ):
            raise ValueError("Managed retirement result differs from requested mode")
        if bool(records[0].get("retire_response_loss_recovered")) is not True:
            raise ValueError(
                "Retirement response-loss result differs from requested mode"
            )
        if records[0].get("retire_response_losses") != (
            len(workers) if inject_retire_loss else 0
        ):
            raise ValueError("Retirement response-loss count differs")
        if (
            records[0].get("churn_families") != churn_families
            or records[0].get("churn_children") != churn_families * churn_children
        ):
            raise ValueError("Retirement churn result differs")
        report["retirement_fault_proxies"] = [
            {"dropped": proxy.dropped, "forwarded": proxy.forwarded}
            for proxy in retirement_proxies
        ]
        if inject_retire_loss and any(
            item["dropped"] != 1 or item["forwarded"] < 3
            for item in report["retirement_fault_proxies"]
        ):
            raise ValueError("Retirement fault proxy coverage differs")
        report.update(status="pass", chain=records[0])
        if exercise_control:
            coverage = records[0].get("control_gate_qualification", {})
            if (
                not all(
                    coverage.get(key) is True
                    for key in (
                        "http_control_failure",
                        "same_source_revalidated",
                        "debt_preserved",
                        "health_expiry",
                        "identity_quarantined",
                        "other_replica_serves",
                        "drain_rejected",
                    )
                )
                or control_proxies[0].failures == 0
                or control_proxies[0].conflicts == 0
            ):
                report["status"] = "fail"
                raise ValueError("Control fault coverage incomplete")
            report["control_faults"] = [
                {
                    "http_failures": p.failures,
                    "injected_identity_conflicts": p.conflicts,
                }
                for p in control_proxies
            ]
    finally:
        if process is not None:
            report["router_cleanup"] = stop_owned_group(process)
        report["cleanup"] = []
        errors = []
        for proxy in reversed(retirement_proxies + control_proxies):
            try:
                proxy.stop()
            except Exception as error:
                errors.append(str(error))
        for backend in reversed(backends):
            try:
                if backend.process is not None:
                    backend.stop()
                    if backend.cleanup is None:
                        errors.append("backend cleanup result is missing")
                    elif backend.cleanup["forced_group_cleanup"]:
                        errors.append("backend required forced group cleanup")
            except Exception as error:
                errors.append(str(error))
            report["cleanup"].append(backend.cleanup)
        if errors:
            raise RuntimeError("Owned backend cleanup failed: " + "; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pycache-root", type=Path)
    parser.add_argument(
        "--durable-store",
        action="store_true",
        help="Require durable-candidate binary and a new private SQLite store",
    )
    parser.add_argument(
        "--managed-terminal-retirement",
        action="store_true",
        help=(
            "Use the exclusive managed pool and require durable commit, terminal ACK "
            "and idempotent family retirement"
        ),
    )
    parser.add_argument(
        "--inject-retire-response-loss",
        action="store_true",
        help=(
            "Drop the first committed retire reply per backend in a loopback test proxy"
        ),
    )
    parser.add_argument("--churn-families", type=int, default=0)
    parser.add_argument("--exercise-control-gate", action="store_true")
    parser.add_argument("--churn-children-per-family", type=int, default=0)
    parser.add_argument(
        "--exercise-router-recovery",
        action="store_true",
        help=(
            "Force exit after durable send, recover paused, reject old cursors, "
            "then replace one HUST control instance and require fail-closed binding"
        ),
    )
    parser.add_argument("--port", type=int, default=18170)
    args = parser.parse_args()
    if os.name != "posix" or not 0 < args.port < 65535:
        parser.error("Linux and two consecutive loopback ports required")
    args.model = args.model.resolve(strict=True)
    args.binary = private_path(args.binary).resolve(strict=True)
    if args.managed_terminal_retirement and not args.durable_store:
        parser.error("--managed-terminal-retirement requires --durable-store")
    if args.inject_retire_response_loss and not args.managed_terminal_retirement:
        parser.error("--inject-retire-response-loss requires managed retirement")
    if args.exercise_router_recovery and not (
        args.managed_terminal_retirement
        and args.durable_store
        and not args.inject_retire_response_loss
        and args.churn_families == 0
    ):
        parser.error(
            "--exercise-router-recovery requires managed durable mode "
            "without churn/loss"
        )
    output = private_path(args.output)
    for port in (args.port, args.port + 1):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "fail",
        "scope": "finite-real-public-http-chain",
        "performance_verified": False,
        "durable_recovery_verified": False,
    }
    try:
        run(args, output, report)
    except Exception as error:
        report.update(status="fail", error=f"{type(error).__name__}: {error}")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "error": report.get("error"),
            }
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
