"""Finite real public HTTP chain qualification on two owned HUST TP1 services.

Launch, exact profile checks and cleanup reuse existing qualification utilities.
The Rust binary owns M1/M2/M3/M4/M5/M7; Python is not an inference proxy.
"""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

from prefix_routing_mod.adapters.profiles import inspect_backend
from prefix_routing_mod.terminal_transport import FrontendEnds
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


def run(args, output, report):
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
    backends, workers = [], []
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
                ),
                root / "backend",
                opener,
                origin,
            )
            backends.append(backend)
            backend.start()
            workers.append(
                {
                    "origin": origin,
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
        }
        config_path = output / "private-runtime-config.json"
        with os.fdopen(
            os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            json.dump(config, stream)
        with args.binary.open("rb") as binary:
            report["binary_sha256"] = hashlib.file_digest(binary, "sha256").hexdigest()
        with (output / "router.log").open("xb") as log:
            process = subprocess.Popen(
                [str(args.binary), str(config_path)],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            process.wait(timeout=180)
        if process.returncode:
            raise RuntimeError(f"Fixed Router chain exited {process.returncode}")
        if (output / "router.log").stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Router evidence log exceeds budget")
        records = []
        for line in (output / "router.log").read_text("utf-8").splitlines():
            if line.startswith("{"):
                value = json.loads(line)
                if value.get("scope") == "finite-public-http-real-M1-M2-M3-M4-M5-M7":
                    records.append(value)
        if len(records) != 1 or records[0].get("status") != "pass":
            raise ValueError("Missing complete public-chain report")
        report.update(status="pass", chain=records[0])
    finally:
        if process is not None:
            report["router_cleanup"] = stop_owned_group(process)
        report["cleanup"] = []
        errors = []
        for backend in reversed(backends):
            try:
                backend.stop()
                if backend.cleanup["forced_group_cleanup"]:
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
    parser.add_argument("--port", type=int, default=18170)
    args = parser.parse_args()
    if os.name != "posix" or not 0 < args.port < 65535:
        parser.error("Linux and two consecutive loopback ports required")
    args.model = args.model.resolve(strict=True)
    args.binary = private_path(args.binary).resolve(strict=True)
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
