"""Finite two-TP1 inference stability diagnostic using the pinned HUST stack.

Reuses existing launch and cleanup. This measures backend request stability;
it does not install terminal hooks, build a directory or claim Router capacity.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from prefix_routing_mod.adapters.profiles import inspect_backend
from tools.hust_native_probe import PROFILE, cached_tokens, command, service_environment
from tools.live_terminal_cases import MODEL, request_json
from tools.regress_kv_recovery import OwnedBackend
from tools.restore_runtime import private_path


def exercise(
    opener, origin, replica, deadline, progress, stop, *, clock=time.monotonic
):
    """One in-flight request per replica, bounded counters, no response retention."""
    summary = {
        "replica": replica,
        "requests": 0,
        "generated_tokens": 0,
        "cached_tokens": 0,
        "latency_seconds_sum": 0.0,
        "latency_seconds_max": 0.0,
    }
    last_report = clock()
    while not stop.is_set() and clock() < deadline:
        budget = (16, 64, 128)[summary["requests"] % 3]
        started = clock()
        reply = request_json(
            opener,
            origin,
            "/v1/completions",
            body={
                "model": MODEL,
                "prompt": "A reusable prefix supports efficient inference. " * 64,
                "max_tokens": budget,
                "temperature": 0,
                "ignore_eos": True,
            },
            timeout=60,
        )
        elapsed = clock() - started
        usage = reply.get("usage", {})
        generated = usage.get("completion_tokens")
        cached = cached_tokens(usage)
        if (
            type(generated) is not int
            or generated != budget
            or cached is None
            or not reply.get("choices")
        ):
            raise ValueError("Actual completion or cache accounting differs")
        summary["requests"] += 1
        summary["generated_tokens"] += generated
        summary["cached_tokens"] += cached
        summary["latency_seconds_sum"] += elapsed
        summary["latency_seconds_max"] = max(summary["latency_seconds_max"], elapsed)
        if clock() - last_report >= 30:
            progress(summary.copy())
            last_report = clock()
        if summary["requests"] >= 10000:
            raise RuntimeError("Stability request budget exhausted before deadline")
    progress(summary.copy())
    return summary


def run(args, output, report):
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
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    backends = []
    try:
        for device in (0, 1):
            root = output / f"replica-{device}"
            root.mkdir()
            origin = f"http://127.0.0.1:{args.port + device}"
            backend = OwnedBackend(
                command(
                    Path(sys.executable).with_name("vllm"),
                    args.model,
                    args.port + device,
                    root,
                ),
                service_environment(device, root, acl, pycache_root=args.pycache_root),
                root / "backend",
                opener,
                origin,
            )
            backends.append(backend)
            backend.start()
        report["both_replicas_running"] = all(
            b.process.poll() is None for b in backends
        )
        started = time.monotonic()
        deadline = started + args.duration_seconds
        stop = threading.Event()

        def progress(value):
            target = output / f"progress-{value['replica']}.json"
            target.write_text(json.dumps(value, indent=2) + "\n", "utf-8")

        def execute(replica, backend):
            try:
                return exercise(
                    opener, backend.origin, replica, deadline, progress, stop
                )
            except BaseException:
                stop.set()
                raise

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(execute, replica, b) for replica, b in enumerate(backends)
            ]
            report["replicas"] = [future.result() for future in futures]
        report["elapsed_seconds"] = time.monotonic() - started
        report["status"] = "pass"
    finally:
        report["cleanup"] = []
        errors = []
        for backend in reversed(backends):
            try:
                backend.stop()
            except Exception as error:
                errors.append(str(error))
            report["cleanup"].append(backend.cleanup)
        if errors:
            raise RuntimeError("Owned backend cleanup failed: " + "; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pycache-root", type=Path)
    parser.add_argument("--duration-seconds", type=int, default=1200)
    parser.add_argument("--port", type=int, default=18180)
    args = parser.parse_args()
    if (
        os.name != "posix"
        or not 0 < args.port < 65535
        or not 1 <= args.duration_seconds <= 1800
    ):
        parser.error(
            "Linux, two consecutive ports and a 1..1800 second duration required"
        )
    args.model = args.model.resolve(strict=True)
    output = private_path(args.output)
    for port in (args.port, args.port + 1):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "fail",
        "scope": "finite-two-TP1-backend-stability",
        "requested_duration_seconds": args.duration_seconds,
        "terminal_observer_enabled": False,
        "full_routing_verified": False,
        "performance_verified": False,
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
