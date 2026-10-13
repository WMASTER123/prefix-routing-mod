"""Actual Router CPU fault/restart probe using one persistent experimental log.

Owned Router groups stop before receiver replacement. This provides process
evidence for this supervisor only; real service identities remain fixtures.
"""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

from tests.fixtures.mock_backend import Handler, mock_backend
from tools.durable_ledger_prototype import DurableLedger
from tools.online_transport_probe import REF
from tools.router_extension_probe import prototype_result
from tools.smoke_backend import stop_owned_group
from tools.transport_receiver_probe import SCHEMA, read_frame, write_frame

ROOT = Path(__file__).resolve().parents[1]


def exchange(port, message):
    with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
        connection.settimeout(1)
        write_frame(connection, message)
        return read_frame(connection, timeout=1)


def phase(
    binary,
    output,
    database,
    workers,
    index,
    *,
    scenario="",
    fault="none",
    crash_receiver=False,
    old_event=None,
):
    output.mkdir()
    token = secrets.token_hex(32)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PREFIX_PROBE_")
    }
    environment["PREFIX_PROBE_TOKEN"] = token
    clock = f"recovery-runtime-{index}"
    command = [
        sys.executable,
        "-m",
        "tools.recovery_receiver_probe",
        "--database",
        str(database),
        "--report",
        str(output / "receiver.json"),
        "--clock",
        clock,
        "--fault",
        fault,
    ]
    if index > 1:
        command.append("--resume")
    receiver = router = None
    reader = None
    report = {"phase": index, "scenario": scenario, "clock_id": clock}
    try:
        with (output / "receiver.log").open("wb") as receiver_log:
            receiver = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=receiver_log,
                start_new_session=True,
            )
            ready = Queue(maxsize=1)
            reader = Thread(
                target=lambda: ready.put(receiver.stdout.readline(8193)), daemon=True
            )
            reader.start()
            line = ready.get(timeout=5)
            if not line or len(line) > 8192:
                raise RuntimeError("bounded receiver readiness missing")
            readiness = json.loads(line)
            port = readiness["port"]
            report["startup"] = readiness["startup"]
            # A live owner excludes a second opener even before dispatch starts.
            try:
                with DurableLedger(database):
                    raise AssertionError("second log owner accepted")
            except OSError:
                report["second_owner_rejected"] = True
            if old_event is not None:
                response = exchange(
                    port, {"schema": SCHEMA, "token": token, "event": old_event}
                )
                if response["result"] != "duplicate":
                    raise AssertionError("old runtime observation not deduplicated")
                report["old_observation"] = "duplicate"
            environment.update(
                PREFIX_PROBE_PORT=str(port),
                PREFIX_PROBE_DYNAMIC="1",
                PREFIX_PROBE_CLOCK=clock,
                PREFIX_PROBE_NAMESPACE=f"run-{index}",
                PREFIX_PROBE_SCENARIO=scenario,
            )
            with (output / "router.log").open("wb") as router_log:
                router = subprocess.Popen(
                    [str(binary), *workers],
                    cwd=ROOT,
                    env=environment,
                    stdout=router_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                router.wait(timeout=30)
            if router.returncode:
                raise RuntimeError(f"Router probe exited {router.returncode}")
            report["router"] = prototype_result(output / "router.log")
            cleanup = stop_owned_group(router)
            if cleanup["forced_group_cleanup"]:
                raise RuntimeError("Router descendants required forced cleanup")
            report["router_stopped_before_receiver"] = True
            if crash_receiver:
                receiver.kill()
                receiver.wait(timeout=5)
                report["receiver_exit"] = "injected_kill"
            else:
                if exchange(
                    port, {"schema": "receiver-stop-cpu-v1", "token": token}
                ) != {"status": "stopping"}:
                    raise RuntimeError("receiver stop not acknowledged")
                receiver.wait(timeout=5)
                if receiver.returncode:
                    raise RuntimeError("receiver stop failed")
                report["receiver"] = json.loads((output / "receiver.json").read_text())
                if report["receiver"]["status"] != "pass":
                    raise RuntimeError("receiver lifetime expired")
            with DurableLedger(database) as store:
                report["reopened"] = {
                    "outstanding": store.outstanding(REF),
                    "reservations": len(store.oracle._attempts),
                    "receipts": len(store.transport_receipts),
                    "admission_ready": store.admission_ready,
                }
    finally:
        cleanup_failed = False
        for process in (router, receiver):
            if process is not None:
                cleanup = stop_owned_group(process)
                cleanup_failed |= cleanup["forced_group_cleanup"]
        if receiver is not None and receiver.stdout is not None:
            receiver.stdout.close()
        if reader is not None:
            reader.join(timeout=1)
            cleanup_failed |= reader.is_alive()
        if cleanup_failed:
            raise RuntimeError("owned child cleanup required force or reader survived")
    return report


def run(binary, output):
    if os.name != "posix":
        raise ValueError("actual Router recovery probe requires Linux groups")
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("executable Router probe required")
    output.mkdir(parents=True, exist_ok=False)
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        "status": "fail",
        "binary_sha256": digest,
        "real_hust": False,
        "npu": 0,
        "scenarios": [],
    }
    lock = Lock()
    counts = {}

    class ProbeHandler(Handler):
        def before_reply(self, request):
            with lock:
                model = request["model"]
                counts[model] = counts.get(model, 0) + 1

    try:
        with mock_backend(ProbeHandler) as a, mock_backend(ProbeHandler) as b:
            for fault in ("lost", "cancel"):
                directory = output / fault
                directory.mkdir()
                database = directory / "receipts.sqlite"
                initial = phase(
                    binary,
                    directory / "initial",
                    database,
                    (a, b),
                    1,
                    scenario=f"admission-{fault}",
                    fault=fault,
                    crash_receiver=fault == "cancel",
                )
                recovered = phase(binary, directory / "recovered", database, (a, b), 2)
                lines = (
                    (directory / "recovered" / "router.log").read_text().splitlines()
                )
                transcript = [
                    json.loads(line)
                    for line in lines
                    if line.startswith('{"lifecycle_schema":')
                ]
                old_event = transcript[0]["traces"][0][0]
                saturated = phase(
                    binary,
                    directory / "saturated",
                    database,
                    (a, b),
                    3,
                    scenario="recover-quota",
                    old_event=old_event,
                )
                for item, expected in (
                    (initial, (1, 1, 0)),
                    (recovered, (2, 2, 2)),
                    (saturated, (2, 2, 2)),
                ):
                    opened = item["reopened"]
                    if (
                        opened["outstanding"],
                        opened["reservations"],
                        opened["receipts"],
                    ) != expected or opened["admission_ready"]:
                        raise AssertionError("restart lost debt or reopened admission")
                if (
                    recovered["startup"]["outstanding"] != 1
                    or saturated["startup"]["outstanding"] != 2
                ):
                    raise AssertionError("runtime handover cleared debt")
                report["scenarios"].append(
                    {"fault": fault, "phases": [initial, recovered, saturated]}
                )
        report["backend_attempts"] = counts
        # Every Router starts with two default-off calls and one 503; only the
        # recovered middle phase adds an extension send (two such phases).
        if counts != {"fixture": 14, "fixture-error": 6}:
            raise AssertionError("unexpected backend send after missing permit")
        report["status"] = "pass"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    with (output / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.binary, args.output)
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
