"""Bounded child receiver for Router restart experiments, CPU fixtures only.

The supervisor must stop its old Router process group before passing --resume.
Fresh worker health/identity here are synthetic, not real HUST observations.
"""

import argparse
import hmac
import json
import os
import socket
import time
from dataclasses import replace
from pathlib import Path

from prefix_routing_mod.contracts import ReadContext
from tools.durable_ledger_prototype import DurableLedger
from tools.online_transport_probe import REF, AdmissionReceiver, fixture_worker
from tools.transport_receiver_probe import read_frame, write_frame


def summary(store):
    attempts = store.oracle._attempts.values()
    return {
        "reservations": len(store.oracle._attempts),
        "receipts": len(store.transport_receipts),
        "outstanding": store.outstanding(REF),
        "reserved_without_observation": sum(not a.events for a in attempts),
        "unknown": sum(a.state == "uncertain" for a in attempts),
        "admission_ready": store.admission_ready,
    }


def serve(database, report_path, clock, token, *, resume=False, fault="none"):
    if fault not in ("none", "lost", "cancel"):
        raise ValueError("unknown admission fault")
    report = {
        "status": "fail",
        "clock_id": clock,
        "fault": fault,
        "admission_faults": 0,
        "rejected_connections": 0,
    }
    state = replace(
        fixture_worker(),
        clock_id=clock,
        backend_running=replace(fixture_worker().backend_running, clock_id=clock),
        local_outstanding=replace(fixture_worker().local_outstanding, clock_id=clock),
    )
    with DurableLedger(
        database,
        create=not resume,
        workers=(state,) if not resume else (),
        per_worker_limit=2,
    ) as store:
        report["paused_on_reopen"] = not store.admission_ready
        if resume:
            old = store.oracle.snapshot()[0]
            fresh = replace(
                old,
                version=old.version + 1,
                clock_id=clock,
                at_ns=0,
                backend_running=replace(old.backend_running, clock_id=clock, at_ns=0),
                local_outstanding=replace(
                    old.local_outstanding, clock_id=clock, at_ns=0
                ),
            )
            store.resume(
                (fresh,),
                ReadContext(clock, 0, 300_000_000_000),
                previous_dispatcher_stopped=True,
            )
        report["startup"] = summary(store)

        def after_admission():
            if fault == "none" or report["admission_faults"]:
                return
            report["admission_faults"] += 1
            store.admission_ready = False
            if fault == "lost":
                raise ConnectionError("injected admission ACK loss after commit")
            time.sleep(0.5)  # Explicit fixture stall, never production callback.

        receiver = AdmissionReceiver(
            store, token, clock_id=clock, after_admission=after_admission
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            listener.settimeout(0.1)
            print(
                json.dumps(
                    {"port": listener.getsockname()[1], "startup": report["startup"]}
                ),
                flush=True,
            )
            deadline = time.monotonic() + 45
            stopped = False
            while time.monotonic() < deadline:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(0.5)
                    try:
                        message = read_frame(connection)
                        if (
                            isinstance(message, dict)
                            and set(message) == {"schema", "token"}
                            and message["schema"] == "receiver-stop-cpu-v1"
                            and isinstance(message["token"], str)
                            and hmac.compare_digest(message["token"], token)
                        ):
                            write_frame(connection, {"status": "stopping"})
                            stopped = True
                            break
                        write_frame(connection, receiver.handle(message))
                    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                        report["rejected_connections"] += 1
            report["final"] = summary(store)
            report["status"] = "pass" if stopped else "fail"
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--clock", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--fault", choices=("none", "lost", "cancel"), default="none")
    args = parser.parse_args()
    report = serve(
        args.database,
        args.report,
        args.clock,
        os.environ["PREFIX_PROBE_TOKEN"],
        resume=args.resume,
        fault=args.fault,
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
