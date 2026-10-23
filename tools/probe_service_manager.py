"""Linux CPU qualification of the fixed Manager and shared Rust service boundary.

Only launches its own finite fixture processes. No HUST imports, NPU use,
manifest activation, raw prompts or production compatibility claims.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    # A concurrent writer may not yet have emitted the final newline.
    return [
        json.loads(line)
        for line in path.read_text().splitlines(keepends=True)
        if line.endswith("\n") and line.strip()
    ]


def wait_ready(process: subprocess.Popen, output: Path) -> dict:
    deadline = time.monotonic() + 10
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"fixture exited early: {process.returncode}")
        rows = read_rows(output)
        if rows:
            try:
                with opener.open(rows[0]["origin"] + "/readyz", timeout=0.2) as reply:
                    if reply.status == 200:
                        return rows[0]
            except (urllib.error.URLError, TimeoutError):
                pass
        time.sleep(0.01)
    raise AssertionError("fixture startup timed out")


def run_case(binary: Path, directory: Path, mode: str) -> dict:
    store = directory / "ledger.sqlite"
    output = directory / "stdout.jsonl"
    errors = directory / "stderr.txt"
    debt = mode != "clean"
    drain_ms = 150 if mode == "drain_timeout" else 5000
    grace = 0.2 if mode == "manager_timeout" else 6.0
    command = [
        sys.executable,
        "-m",
        "tools.probe_service_manager",
        "--supervise",
        "--grace",
        str(grace),
        "--binary",
        str(binary),
        "--store",
        str(store),
        "--mode",
        "debt" if debt else "fresh",
        "--drain-ms",
        str(drain_ms),
    ]
    child_pid = None
    child_identity = None
    with output.open("w") as out, errors.open("w") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err)
        try:
            start = wait_ready(process, output)
            child_pid = start["pid"]
            child_identity = (
                Path(f"/proc/{child_pid}/stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()[19]
            )
            process.send_signal(signal.SIGTERM)
            if mode == "second_signal":
                # Wait until the service actually withdrew readiness. The first
                # signal is thus known to have reached the child before escalating.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 2
                while True:
                    try:
                        opener.open(start["origin"] + "/readyz", timeout=0.2).close()
                    except urllib.error.HTTPError as exc:
                        if exc.code == 503:
                            break
                        raise
                    if time.monotonic() >= deadline:
                        raise AssertionError("first signal was not observed")
                    time.sleep(0.01)
                process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=9) == 128 + signal.SIGTERM
            assert not Path(f"/proc/{child_pid}").exists(), (
                "Manager left its child alive"
            )
            child_pid = None
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            # Only the group launched for this exact private fixture is owned.
            if child_pid is not None:
                with suppress(FileNotFoundError, ProcessLookupError):
                    current = (
                        Path(f"/proc/{child_pid}/stat")
                        .read_text()
                        .rsplit(")", 1)[1]
                        .split()[19]
                    )
                    if current == child_identity:
                        os.killpg(child_pid, signal.SIGKILL)
    rows = read_rows(output)
    if mode in {"clean", "drain_timeout"}:
        assert rows[-1]["shutdown"] == ("Clean" if mode == "clean" else "TimedOut")
    else:
        assert len(rows) == 1, "forced exit must not claim a clean drain"
    restored = subprocess.run(
        [str(binary), str(store), "recover", "100"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    recovery = json.loads(restored.stdout)
    assert recovery == {"paused": True, "active": int(debt), "through": 0}
    return {"case": mode, "status": "pass", "recovery": recovery}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--grace", type=float, default=6.0, help=argparse.SUPPRESS)
    parser.add_argument("--store", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("fresh", "debt"), help=argparse.SUPPRESS)
    parser.add_argument("--drain-ms", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("requires Linux process groups and POSIX signals")
    binary = args.binary.resolve(strict=True)
    distribution = importlib.metadata.distribution("vllm-hust-ext")
    source = json.loads(distribution.read_text("direct_url.json") or "{}")
    if (
        source.get("vcs_info", {}).get("commit_id")
        != "00d1b940dc6ce8d60e2cd28708b80d80e9c33a09"
    ):
        parser.error("Manager must be installed from the pinned reviewed commit")
    if args.supervise:
        from vllm_hust_ext.process_supervisor import supervise

        return supervise(
            [str(binary), str(args.store), args.mode, str(args.drain_ms)],
            env=dict(os.environ),
            shutdown_grace_seconds=args.grace,
        )
    cases = []
    with tempfile.TemporaryDirectory(prefix="prefix-service-manager-") as root:
        for mode in ("clean", "drain_timeout", "second_signal", "manager_timeout"):
            directory = Path(root) / mode
            directory.mkdir()
            cases.append(run_case(binary, directory, mode))
    print(json.dumps({"scope": "cpu-substitute-manager-signals", "cases": cases}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
