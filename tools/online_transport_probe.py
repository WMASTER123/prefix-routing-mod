"""Finite CPU receiver fixture for the actual Router example process.

Reservations are synthetic and committed BEFORE launching the producer. They
are not inferred from callbacks. Unused reservations are retained and reported.
"""

import os
import secrets
import socket
from contextlib import contextmanager
from dataclasses import asdict
from queue import Queue
from threading import Event, Thread

from prefix_routing_mod.contracts import (
    Health,
    Observation,
    ReadContext,
    RouteDecision,
    WorkerRef,
    WorkerState,
)
from tools.durable_ledger_prototype import DurableLedger
from tools.transport_receiver_probe import Receiver

REF = WorkerRef("fixture-target", "fixture-instance-1")
CLOCK = "fixture-router-session"


def bootstrap(path):
    worker = WorkerState(
        REF,
        "fixture-domain",
        0,
        Health.HEALTHY,
        False,
        CLOCK,
        0,
        Observation(None, "fixture", "whole_instance", CLOCK, 0),
        Observation(0, "local_ledger", "router_session", CLOCK, 0),
    )
    with DurableLedger(path, create=True, workers=(worker,)) as store:
        operations = []
        for index in range(1, 65):
            decision = RouteDecision(
                f"decision-{index}",
                f"request-{index}",
                f"attempt-{index}",
                REF,
                0,
                "fixture",
                "config-1",
                "fixture_only",
            )
            operations.append(
                {
                    "kind": "reserve",
                    "request_id": decision.request_id,
                    "domain": "fixture-domain",
                    "decision": asdict(decision),
                    "context": asdict(ReadContext(CLOCK, 0, 1)),
                }
            )
        store.receive_batch(tuple(operations))


@contextmanager
def online_receiver(path, producer_alive):
    bootstrap(path)
    token = secrets.token_hex(32)
    stopping, ready = Event(), Queue(maxsize=1)
    report = {"online_applied_while_producer_alive": 0, "rejected_connections": 0}
    failures = []

    def run():
        try:
            with DurableLedger(path) as store, socket.socket() as listener:

                def committed():
                    if producer_alive():
                        report["online_applied_while_producer_alive"] += 1

                receiver = Receiver(store, token, committed)
                listener.bind(("127.0.0.1", 0))
                listener.listen(8)
                listener.settimeout(0.1)
                ready.put(listener.getsockname()[1])
                while not stopping.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with connection:
                        connection.settimeout(0.5)
                        try:
                            receiver.exchange(connection)
                        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                            report["rejected_connections"] += 1
                report["receipts"] = len(store.transport_receipts)
                attempts = store.oracle._attempts
                report["unused_fixture_reservations"] = sum(
                    not a.events for a in attempts.values()
                )
                report["transport_unknown"] = sum(
                    a.state == "uncertain" for a in attempts.values()
                )
                report["not_sent"] = sum(
                    a.state == "released" for a in attempts.values()
                )
                report["outstanding_including_unused"] = store.outstanding(REF)
                report["admission_ready"] = store.admission_ready
        except BaseException as error:
            failures.append(error)
            if ready.empty():
                ready.put(None)

    thread = Thread(target=run, name="cpu-transport-receiver", daemon=True)
    thread.start()
    try:
        port = ready.get(timeout=5)
        if port is None:
            raise RuntimeError("receiver startup failed") from failures[0]
        environment = dict(
            os.environ, PREFIX_PROBE_PORT=str(port), PREFIX_PROBE_TOKEN=token
        )
        yield environment, report
    finally:
        stopping.set()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("receiver failed to stop within budget")
        if failures:
            raise RuntimeError("receiver failed") from failures[0]
