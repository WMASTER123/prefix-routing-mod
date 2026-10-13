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
    Precision,
    ReadContext,
    RequestFeatures,
    ReservationRejected,
    RouteDecision,
    WorkerRef,
    WorkerState,
)
from tools.durable_ledger_prototype import DurableLedger
from tools.transport_receiver_probe import SCHEMA as TRANSPORT_SCHEMA
from tools.transport_receiver_probe import Receiver

REF = WorkerRef("fixture-target", "fixture-instance-1")
CLOCK = "fixture-router-session"


def fixture_worker():
    return WorkerState(
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


def bootstrap(path):
    with DurableLedger(path, create=True, workers=(fixture_worker(),)) as store:
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


class AdmissionReceiver(Receiver):
    """One CPU owner for admission and observations, not a production service.

    Only this fixture's fixed worker/domain/config are accepted. The caller's
    clock is synthetic; no cross-host timestamp or real health claim is made.
    """

    def handle(self, message):
        if isinstance(message, dict) and message.get("schema") == TRANSPORT_SCHEMA:
            return super().handle(message)
        import hmac

        if (
            not isinstance(message, dict)
            or set(message)
            != {"schema", "token", "request_id", "attempt_id", "clock_id", "at_ns"}
            or message["schema"] != "admission-cpu-v1"
            or not isinstance(message["token"], str)
            or not hmac.compare_digest(message["token"], self.token)
            or message["clock_id"] != CLOCK
        ):
            raise ValueError("invalid admission capability or binding")
        context = ReadContext(CLOCK, message["at_ns"], 300_000_000_000)
        features = RequestFeatures(
            message["request_id"],
            "fixture-domain",
            None,
            None,
            None,
            None,
            Precision.UNKNOWN,
        )
        decision = RouteDecision(
            message["attempt_id"],
            message["request_id"],
            message["attempt_id"],
            REF,
            0,
            "fixture",
            "config-1",
            "fixture_only",
        )
        try:
            result = self.store.reserve(features, decision, context)
        except ReservationRejected:
            result = "rejected"
        response = {key: value for key, value in message.items() if key != "token"}
        response["result"] = result
        return response


@contextmanager
def online_receiver(path, producer_alive, *, lose_first_ack=False, dynamic=False):
    if dynamic and lose_first_ack:
        raise ValueError("separate admission and observation fault experiments")
    if not dynamic:
        bootstrap(path)
    token = secrets.token_hex(32)
    stopping, ready = Event(), Queue(maxsize=1)
    report = {"ack_attempts_while_producer_alive": 0, "rejected_connections": 0}
    failures = []

    def run():
        try:
            with (
                DurableLedger(
                    path,
                    create=dynamic,
                    workers=(fixture_worker(),) if dynamic else (),
                    per_worker_limit=1 if dynamic else 64,
                ) as store,
                socket.socket() as listener,
            ):
                lost_ack = False

                def committed():
                    nonlocal lost_ack
                    if producer_alive():
                        report["ack_attempts_while_producer_alive"] += 1
                    if lose_first_ack and not lost_ack:
                        lost_ack = True
                        raise ConnectionError("injected loss after commit")

                receiver_type = AdmissionReceiver if dynamic else Receiver
                receiver = receiver_type(store, token, committed)
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
                report["reservations"] = len(store.oracle._attempts)
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
        environment.pop("PREFIX_PROBE_FAULT_DROP_ACK", None)
        environment.pop("PREFIX_PROBE_DYNAMIC", None)
        if dynamic:
            environment["PREFIX_PROBE_DYNAMIC"] = "1"
        if lose_first_ack:
            environment["PREFIX_PROBE_FAULT_DROP_ACK"] = "1"
        yield environment, report
    finally:
        stopping.set()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("receiver failed to stop within budget")
        if failures:
            raise RuntimeError("receiver failed") from failures[0]
