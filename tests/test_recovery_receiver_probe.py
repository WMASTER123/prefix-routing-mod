"""Real child receiver faults/reopen; full native Router is a manual probe."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from queue import Queue
from threading import Thread

import pytest

from tools.durable_ledger_prototype import DurableLedger
from tools.online_transport_probe import REF
from tools.transport_receiver_probe import read_frame, write_frame

TOKEN = "ab" * 32


def child(path, report, clock, *, resume=False, fault="none", commit_signal=None):
    command = [
        sys.executable,
        "-m",
        "tools.recovery_receiver_probe",
        "--database",
        str(path),
        "--report",
        str(report),
        "--clock",
        clock,
        "--fault",
        fault,
    ]
    if resume:
        command.append("--resume")
    if commit_signal is not None:
        command.extend(("--commit-signal", str(commit_signal)))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, PREFIX_PROBE_TOKEN=TOKEN),
    )
    ready = Queue(maxsize=1)
    reader = Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        line = ready.get(timeout=5)
        if not line:
            raise RuntimeError("child readiness missing")
        return process, json.loads(line)["port"]
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        raise
    finally:
        reader.join(timeout=1)


def exchange(port, message):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.settimeout(2)
        write_frame(connection, message)
        return read_frame(connection, timeout=2)


def stop(process, port):
    try:
        exchange(port, {"schema": "receiver-stop-cpu-v1", "token": TOKEN})
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def admission(clock, index=1):
    return {
        "schema": "admission-cpu-v1",
        "token": TOKEN,
        "request_id": f"request-{clock}-{index}",
        "attempt_id": f"attempt-{clock}-{index}",
        "clock_id": clock,
        "at_ns": 10,
    }


@pytest.mark.parametrize("fault", ["lost", "cancel"])
def test_unconfirmed_reservation_survives_real_receiver_restart(tmp_path, fault):
    path = tmp_path / "log"
    first_report = tmp_path / "first.json"
    process, port = child(path, first_report, "runtime-a", fault=fault)
    try:
        if fault == "lost":
            with pytest.raises(ConnectionError):
                exchange(port, admission("runtime-a"))
        else:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                # Caller never receives this committed reservation's permit.
                write_frame(connection, admission("runtime-a"))
        assert exchange(port, admission("runtime-a", 2))["result"] == "rejected"
    finally:
        stop(process, port)
    report = json.loads(first_report.read_text())
    assert report["admission_faults"] == 1
    assert report["final"]["outstanding"] == 1
    assert not report["final"]["admission_ready"]
    with DurableLedger(path) as store:
        assert not store.admission_ready and store.outstanding(REF) == 1
    second_report = tmp_path / "second.json"
    process, port = child(path, second_report, "runtime-b", resume=True)
    try:
        with pytest.raises(OSError):
            DurableLedger(path)
        with pytest.raises(ConnectionError):
            exchange(port, admission("runtime-a", 3))
        assert exchange(port, admission("runtime-b"))["result"] == "applied"
        assert exchange(port, admission("runtime-b", 2))["result"] == "rejected"
    finally:
        stop(process, port)
    with DurableLedger(path) as store:
        assert not store.admission_ready and store.outstanding(REF) == 2
        assert len(store.oracle._attempts) == 2
    second = json.loads(second_report.read_text())
    assert second["paused_on_reopen"] and second["startup"]["outstanding"] == 1


def test_missing_database_never_starts_receiver(tmp_path):
    with pytest.raises(RuntimeError, match="readiness"):
        child(tmp_path / "missing", tmp_path / "report", "runtime-b", resume=True)
    assert not (tmp_path / "missing").exists()


def test_native_probe_requires_linux_and_executable(tmp_path):
    from tools.router_recovery_probe import run

    if os.name == "posix":
        invalid = tmp_path / "binary"
        invalid.write_text("not executable")
        invalid.chmod(0o600)
        with pytest.raises(ValueError, match="executable"):
            run(invalid, tmp_path / "output")
    else:
        with pytest.raises(ValueError, match="Linux"):
            run(Path("not-a-binary"), tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_recovery_rejects_oversized_or_missing_transcript(tmp_path):
    from tools.router_extension_probe import MAX_LOG_BYTES
    from tools.router_recovery_probe import read_transcript

    path = tmp_path / "log"
    path.write_bytes(b"x" * (MAX_LOG_BYTES + 1))
    with pytest.raises(ValueError, match="budget"):
        read_transcript(path)
    path.write_text('{"status":"pass"}\n')
    with pytest.raises(ValueError, match="one lifecycle"):
        read_transcript(path)


def test_two_way_barrier_prevents_admission_ack_until_caller_cancels(tmp_path):
    signal = tmp_path / "committed"
    report = tmp_path / "report.json"
    process, port = child(
        tmp_path / "log", report, "runtime-a", fault="cancel", commit_signal=signal
    )
    connection = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        write_frame(connection, admission("runtime-a"))
        deadline = time.monotonic() + 3
        while not signal.is_file():
            assert time.monotonic() < deadline, "post-commit signal missing"
            time.sleep(0.005)
        connection.settimeout(0.05)
        with pytest.raises(TimeoutError):
            connection.recv(1)
        # The commit marker precedes cancellation, and the response follows it.
        connection.close()
        signal.with_name(signal.name + ".cancelled").write_bytes(b"cancelled\n")
    finally:
        connection.close()
        stop(process, port)
    assert json.loads(report.read_text())["final"]["outstanding"] == 1
