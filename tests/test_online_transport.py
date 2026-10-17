"""Receipt/order/fault tests of the same ledger used by the native callback sink."""

import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from queue import Queue
from threading import Thread

import pytest

from prefix_routing_mod.contracts import (
    BackendConfirmation,
    EventKind,
    LifecycleRejected,
)
from tests.test_component_contracts import (
    CONTEXT,
    FEATURES,
    REF,
    decision,
    event,
    worker,
)
from tools.durable_ledger_prototype import DurableLedger
from tools.terminal_observer_prototype import Binding, Ended
from tools.transport_receiver_probe import SCHEMA, Receiver, read_frame, write_frame

TOKEN = "ab" * 32


def open_store(path, **kwargs):
    store = DurableLedger(path, create=True, workers=(worker(),), **kwargs)
    store.reserve(FEATURES, decision(), CONTEXT)
    return store


def source(kind=EventKind.SEND_STARTED, **changes):
    if kind is EventKind.OUTCOME_UNKNOWN:
        return event(kind, sequence=2, error_class="transport_eof", **changes)
    if kind is EventKind.NOT_SENT:
        return event(kind, BackendConfirmation.NOT_ACCEPTED, **changes)
    return event(kind, **changes)


def envelope(record):
    return {"schema": SCHEMA, "token": TOKEN, "event": asdict(record)}


def test_online_receipts_restart_and_duplicates(tmp_path):
    path = tmp_path / "log.sqlite"
    with open_store(path) as store:
        receiver = Receiver(store, TOKEN)
        assert receiver.handle(envelope(source()))["result"] == "applied"
        assert store.outstanding(REF) == 1
    with DurableLedger(path) as store:
        receiver = Receiver(store, TOKEN)
        assert not store.admission_ready
        assert receiver.handle(envelope(source()))["result"] == "duplicate"
        assert (
            receiver.handle(envelope(source(EventKind.OUTCOME_UNKNOWN)))["result"]
            == "applied"
        )
        assert store.oracle.uncertain("attempt-a")
        assert store.operation_count == 3


def test_receiver_restart_can_observe_old_send_without_authorizing_send(tmp_path):
    path = tmp_path / "log.sqlite"
    with open_store(path):
        pass
    with DurableLedger(path) as store:
        assert store.receive_transport(source()) == "applied"
        assert not store.admission_ready
        assert store.outstanding(REF) == 1


def test_lost_ack_reuses_committed_observation(tmp_path):
    def lose():
        raise ConnectionError("injected loss after durable commit")

    path = tmp_path / "log.sqlite"
    with open_store(path) as store:
        with pytest.raises(ConnectionError):
            Receiver(store, TOKEN, lose).handle(envelope(source()))
        assert store.operation_count == 2
    with DurableLedger(path) as store:
        assert (
            Receiver(store, TOKEN).handle(envelope(source()))["result"] == "duplicate"
        )
        assert store.operation_count == 2 and store.outstanding(REF) == 1


def test_backend_terminal_interleaves_with_late_transport_close(tmp_path):
    path = tmp_path / "log.sqlite"
    with open_store(path) as store:
        store.receive_transport(source())
        ended = Ended(
            Binding(REF.worker_id, REF.instance_epoch, "observer"), 1, "attempt-a"
        )
        store.confirm_end(
            ended, EventKind.CANCEL_CONFIRMED, replace(CONTEXT, now_ns=110)
        )
        assert store.outstanding(REF) == 0
        # Source time is older than the committed terminal time. This is a
        # receipt-only operation, not an illegal backward lifecycle transition.
        assert store.receive_transport(source(EventKind.OUTCOME_UNKNOWN)) == "applied"
        assert len(store.oracle._attempts["attempt-a"].events) == 2
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == 0
        assert store.receive_transport(source(EventKind.OUTCOME_UNKNOWN)) == "duplicate"


@pytest.mark.parametrize("kind", [EventKind.NOT_SENT, EventKind.OUTCOME_UNKNOWN])
def test_transport_never_infers_success(tmp_path, kind):
    with open_store(tmp_path / "log.sqlite") as store:
        if kind is EventKind.OUTCOME_UNKNOWN:
            store.receive_transport(source())
        store.receive_transport(source(kind))
        assert store.outstanding(REF) == int(kind is EventKind.OUTCOME_UNKNOWN)
        assert all(
            item.kind is not EventKind.COMPLETED
            for item in store.oracle._attempts["attempt-a"].events
        )


@pytest.mark.parametrize(
    "mutation", ["token", "field", "ended", "gap", "identity", "clock", "reason"]
)
def test_bad_observation_does_not_ack_or_mutate(tmp_path, mutation):
    with open_store(tmp_path / "log.sqlite") as store:
        message = envelope(source())
        if mutation == "token":
            message["token"] = "cd" * 32
        elif mutation == "field":
            message["extra"] = True
        elif mutation == "ended":
            message["event"] = asdict(
                event(EventKind.COMPLETED, BackendConfirmation.ENDED)
            )
        elif mutation == "gap":
            message = envelope(source(EventKind.OUTCOME_UNKNOWN))
        elif mutation == "identity":
            message["event"]["request_id"] = "other"
        elif mutation == "clock":
            message["event"]["clock_id"] = "other"
        else:
            message["event"]["error_class"] = "transport_eof"
        with pytest.raises((ValueError, LifecycleRejected)):
            Receiver(store, TOKEN).handle(message)
        assert store.operation_count == 1 and store.outstanding(REF) == 1


def test_conflicting_duplicate_is_not_acknowledged(tmp_path):
    with open_store(tmp_path / "log.sqlite") as store:
        store.receive_transport(source())
        with pytest.raises(LifecycleRejected, match="reused"):
            store.receive_transport(replace(source(), at_ns=105))
        assert store.operation_count == 2


@pytest.mark.parametrize("point", ["before_commit", "after_commit_before_ack"])
def test_storage_failure_never_returns_receipt(tmp_path, point):
    def fail(where):
        if where == point:
            raise OSError("injected storage fault")

    path = tmp_path / "log.sqlite"
    with open_store(path) as store:
        store.fault = fail
        with pytest.raises(OSError):
            Receiver(store, TOKEN).handle(envelope(source()))
        assert store.failed and not store.admission_ready
    with DurableLedger(path) as store:
        assert store.receive_transport(source()) == (
            "applied" if point == "before_commit" else "duplicate"
        )
        assert store.outstanding(REF) == 1


def test_retention_full_preserves_unresolved_debt(tmp_path):
    with open_store(tmp_path / "log.sqlite", max_operations=1) as store:
        with pytest.raises(RuntimeError, match="retention"):
            Receiver(store, TOKEN).handle(envelope(source()))
        assert store.outstanding(REF) == 1 and store.operation_count == 1


@pytest.mark.parametrize("size", [0, 8193, 2**32 - 1])
def test_frame_limit_checked_before_payload_read(size):
    sender, receiver = socket.socketpair()
    with sender, receiver:
        receiver.settimeout(0.2)
        sender.sendall(struct.pack("!I", size))
        with pytest.raises(ValueError, match="budget"):
            read_frame(receiver)


def test_truncated_frame_is_not_acknowledged():
    sender, receiver = socket.socketpair()
    with sender, receiver:
        sender.sendall(struct.pack("!I", 10) + b"{}")
        sender.shutdown(socket.SHUT_WR)
        with pytest.raises(ConnectionError, match="incomplete"):
            read_frame(receiver)


@contextmanager
def receiver_process(path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tools.transport_receiver_probe",
            "--database",
            str(path),
        ],
        env=dict(os.environ, PREFIX_PROBE_TOKEN=TOKEN),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = Queue(maxsize=1)
    reader = Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        message = ready.get(timeout=5)
        assert message, "receiver exited before binding"
        yield ("127.0.0.1", json.loads(message)["port"])
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()


def test_actual_socket_ack_loss_and_receiver_process_restart(tmp_path):
    path = tmp_path / "log.sqlite"
    with open_store(path):
        pass
    with receiver_process(path) as address:
        with socket.create_connection(address, timeout=1) as connection:
            write_frame(connection, envelope(source()))
            # Discard the ACK and close. Receiver success is established only
            # from its committed log, not from the client closing a socket.
        deadline = time.monotonic() + 3
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as observer:
            while observer.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] < 2:
                assert time.monotonic() < deadline
                time.sleep(0.01)
    with receiver_process(path) as address:
        for record, expected in [
            (source(), "duplicate"),
            (source(EventKind.OUTCOME_UNKNOWN), "applied"),
        ]:
            with socket.create_connection(address, timeout=1) as connection:
                write_frame(connection, envelope(record))
                assert read_frame(connection)["result"] == expected
    with DurableLedger(path) as store:
        assert store.operation_count == 3
        assert store.outstanding(REF) == 1 and not store.admission_ready


def test_actual_socket_rejects_wrong_capability_and_continues(tmp_path):
    path = tmp_path / "log.sqlite"
    with open_store(path):
        pass
    with receiver_process(path) as address:
        with socket.create_connection(address, timeout=1) as connection:
            message = envelope(source())
            message["token"] = "cd" * 32
            write_frame(connection, message)
            with pytest.raises(ConnectionError):
                read_frame(connection)
        with socket.create_connection(address, timeout=1) as connection:
            write_frame(connection, envelope(source(EventKind.NOT_SENT)))
            assert read_frame(connection)["result"] == "applied"
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == 0 and store.operation_count == 2


def test_partial_arrivals_cannot_extend_absolute_frame_deadline(monkeypatch):
    class Slow:
        reads = 0

        def settimeout(self, value):
            assert value > 0

        def recv(self, size):
            self.reads += 1
            return b"\0"

    ticks = iter([0, 0.2, 0.4, 0.6])
    monkeypatch.setattr(
        "tools.transport_receiver_probe.time.monotonic", lambda: next(ticks)
    )
    connection = Slow()
    with pytest.raises(TimeoutError, match="deadline"):
        read_frame(connection, timeout=0.5)
    assert connection.reads == 2


def test_full_fixture_commits_before_launch_and_retains_unused_slots(tmp_path):
    from tools.online_transport_probe import CLOCK, online_receiver
    from tools.online_transport_probe import REF as fixture_ref

    path = tmp_path / "fixture.sqlite"
    with online_receiver(path, lambda: True) as (environment, report):
        record = replace(
            source(EventKind.NOT_SENT),
            request_id="request-1",
            attempt_id="attempt-1",
            worker=fixture_ref,
            clock_id=CLOCK,
        )
        message = envelope(record)
        message["token"] = environment["PREFIX_PROBE_TOKEN"]
        with socket.create_connection(
            ("127.0.0.1", int(environment["PREFIX_PROBE_PORT"])), timeout=1
        ) as connection:
            write_frame(connection, message)
            assert read_frame(connection)["result"] == "applied"
    assert report["not_sent"] == 1 and report["receipts"] == 1
    assert report["unused_fixture_reservations"] == 63
    assert report["outstanding_including_unused"] == 63
    assert report["admission_ready"] is False


def test_probe_validation_failure_cannot_leave_pass_status(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from pathlib import Path
    from types import SimpleNamespace

    from tools import router_extension_probe as probe

    monkeypatch.setattr(
        probe,
        "os",
        SimpleNamespace(name="posix", access=lambda *a: True, X_OK=1, environ={}),
    )
    monkeypatch.setattr(
        probe, "online_receiver", lambda *a, **k: nullcontext(({}, {"receipts": 99}))
    )
    monkeypatch.setattr(probe, "prototype_result", lambda path: {"status": "pass"})
    monkeypatch.setattr(probe, "validate_log", lambda path: {})
    monkeypatch.setattr(
        probe, "stop_owned_group", lambda process: {"forced_group_cleanup": False}
    )
    process = SimpleNamespace(returncode=0, wait=lambda **k: None, poll=lambda: 0)
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **k: process)
    first = True

    def backend(handler):
        nonlocal first
        if first:
            for model in ["fixture"] * 3 + ["fixture-error"]:
                handler.before_reply(None, {"model": model})
            first = False
        return nullcontext("http://127.0.0.1:1")

    monkeypatch.setattr(probe, "mock_backend", backend)
    report = probe.run(
        Path(sys.executable), tmp_path / "report", online=True, lose_first_ack=True
    )
    assert report["status"] == "fail"
    assert "online receipt/debt mismatch" in report["error"]
