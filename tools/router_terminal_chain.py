"""Explicit finite Router-library -> real HUST -> existing ledger experiment.

No listener/process/import work at import time. The loopback receipt endpoint
is experimental control plumbing, not a new production service or proxy.
"""

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict

from prefix_routing_mod.contracts import (
    EventKind,
    Health,
    Observation,
    ReadContext,
    WorkerRef,
    WorkerState,
)
from prefix_routing_mod.terminal_journal import Binding, Ended
from prefix_routing_mod.terminal_transport import END_STATUSES
from tools.durable_ledger_prototype import DurableLedger
from tools.live_terminal_cases import request_json
from tools.online_transport_probe import AdmissionReceiver
from tools.smoke_backend import stop_owned_group
from tools.transport_receiver_probe import read_frame, write_frame


class TerminalSource:
    """Validate a complete fixed-binding batch before mutating the same M3."""

    def __init__(self, read):
        self.read = read
        initial = read(None, 0, 0)
        self.binding = Binding(**initial["binding"])
        self.core_pid = initial["core_pid"]
        self.frontend_pid = initial["frontend_pid"]
        self.cursor = 0
        self.failed = False
        self.outcomes = {}
        self.families = {}
        self.last_reply = None
        if (
            type(initial.get("schema_version")) is not int
            or initial["schema_version"] != 1
            or initial.get("mode") != "replay"
            or type(initial["boundary"]) is not int
            or type(self.core_pid) is not int
            or type(self.frontend_pid) is not int
            or self.core_pid <= 0
            or self.frontend_pid <= 0
            or self.core_pid == self.frontend_pid
            or initial["boundary"] != 0
            or initial["records"]
            or initial["families"]
            or initial.get("disabled") is not False
        ):
            raise ValueError("Fresh independent frontend/core observer required")

    def check(self):
        if self.failed:
            raise RuntimeError("Terminal source fenced")
        try:
            reply = self.read(self.binding.observer_epoch, self.cursor, self.cursor)
            records = reply["records"]
            if (
                type(reply.get("schema_version")) is not int
                or reply["schema_version"] != 1
                or Binding(**reply["binding"]) != self.binding
                or type(reply["core_pid"]) is not int
                or type(reply["frontend_pid"]) is not int
                or reply["core_pid"] != self.core_pid
                or reply["frontend_pid"] != self.frontend_pid
                or reply.get("disabled") is not False
                or reply["mode"] != "replay"
                or not isinstance(records, list)
                or len(records) > 64
                or type(reply["boundary"]) is not int
                or reply["boundary"] != self.cursor + len(records)
                or not isinstance(reply["families"], list)
                or len(reply["families"]) > 64
            ):
                raise ValueError("Terminal source binding or coverage changed")
            parsed = []
            for seq, value in enumerate(records, self.cursor + 1):
                item = Ended(
                    Binding(**value["binding"]), value["sequence"], value["attempt_id"]
                )
                if item.binding != self.binding or item.sequence != seq:
                    raise ValueError("Terminal batch identity or sequence mismatch")
                parsed.append(item)
            families = {}
            for family in reply["families"]:
                name = family["attempt_id"]
                if not isinstance(name, str) or name in families:
                    raise ValueError("Family identity is invalid or repeated")
                families[name] = family
            # Validate every terminal family, including the tail, before the
            # caller can apply the first valid receipt to its ledger.
            for item in parsed:
                family = families.get(item.attempt_id)
                if family is None or not (
                    family["sealed"] is True
                    and family["abandoned"] is False
                    and all(
                        type(family[k]) is int for k in ("expected", "bound", "ended")
                    )
                    and 0 < family["expected"] == family["bound"] == family["ended"]
                    and isinstance(family["core_statuses"], dict)
                    and set(family["core_statuses"]) <= END_STATUSES
                    and all(
                        type(v) is int and v > 0
                        for v in family["core_statuses"].values()
                    )
                    and sum(family["core_statuses"].values()) == family["ended"]
                ):
                    raise ValueError("Incomplete family cannot close the ledger")
            self.last_reply = reply
            return parsed
        except Exception:
            self.failed = True
            raise

    def consume(self, store, context):
        records = self.check()
        for item in records:
            if item.attempt_id not in store.oracle._attempts:
                self.failed = True
                raise ValueError("Terminal source cannot invent a reservation")
        # The Router may not have supplied the independent business result yet.
        for item in records:
            outcome = self.outcomes.get(item.attempt_id)
            attempt = store.oracle._attempts[item.attempt_id]
            if outcome is None or not attempt.events:
                break
            families = [
                f
                for f in self.last_reply["families"]
                if f["attempt_id"] == item.attempt_id
            ]
            if len(families) != 1:
                raise ValueError("Missing unique terminal family")
            family = families[0]
            if outcome is EventKind.CANCEL_CONFIRMED and family["core_statuses"] != {
                "FINISHED_ABORTED": 1
            }:
                raise ValueError("Actual core abort required for controlled disconnect")
            if store.confirm_end(item, outcome, context) != "applied":
                raise ValueError("Expected first core terminal receipt")
            if store.confirm_end(item, outcome, context) != "duplicate":
                raise ValueError("Terminal redelivery is not idempotent")
            self.families[item.attempt_id] = family
            self.cursor = item.sequence


def run(binary, opener, origin, output, terminal_token):
    """Own one finite actual Router example and the existing receipt owner."""
    report = {
        "status": "fail",
        "online_kv_verified": False,
        "performance_verified": False,
    }
    try:
        return _run(binary, opener, origin, output, terminal_token, report)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        with (output / "router-chain-report.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")


def _run(binary, opener, origin, output, terminal_token, report):
    import hashlib

    binary = binary.resolve(strict=True)
    if os.name != "posix" or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Explicit Linux executable required")
    with binary.open("rb") as stream:
        report["binary_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    headers = {"X-Prefix-Routing-Control": terminal_token}

    def read(epoch, after, ack):
        query = {"after": after, "acknowledge": ack}
        if epoch is not None:
            query["observer_epoch"] = epoch
        return request_json(
            opener,
            origin,
            "/prefix-routing/terminal?" + urllib.parse.urlencode(query),
            headers=headers,
            timeout=2,
        )

    source = TerminalSource(read)
    ref = WorkerRef(source.binding.worker_id, source.binding.instance_epoch)
    clock = "router-live-" + uuid.uuid4().hex
    token = secrets.token_hex(32)
    worker = WorkerState(
        ref,
        "live-chain",
        0,
        Health.HEALTHY,
        False,
        clock,
        0,
        Observation(None, "controlled_bootstrap", "whole_instance", clock, 0),
        Observation(0, "local_ledger", "router_session", clock, 0),
    )
    path = output / "router-chain.sqlite"
    stages = []
    report.update(
        binding=asdict(source.binding),
        stages=stages,
        families=source.families,
        core_pid=source.core_pid,
        frontend_pid=source.frontend_pid,
    )
    process = None
    with (
        DurableLedger(
            path, create=True, workers=(worker,), per_worker_limit=1
        ) as store,
        socket.socket() as listener,
    ):
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.01)
        last_ns = 0
        lost_ack = False
        admission_open = True

        def health():
            if not admission_open:
                raise RuntimeError("Managed admission fenced")
            source.check()
            with opener.open(origin + "/health", timeout=1) as response:
                if response.status != 200:
                    raise ValueError("Backend health unavailable")
                response.read(1024)

        def after_admission():
            nonlocal admission_open
            if lost_ack:
                admission_open = False
                raise ConnectionError("Injected post-commit admission ACK loss")

        receiver = AdmissionReceiver(
            store,
            token,
            clock_id=clock,
            worker_ref=ref,
            domain=worker.domain,
            before_reserve=health,
            after_admission=after_admission,
        )
        env = dict(
            os.environ,
            PREFIX_PROBE_PORT=str(listener.getsockname()[1]),
            PREFIX_PROBE_TOKEN=token,
            PREFIX_PROBE_CLOCK=clock,
            PREFIX_CHAIN_WORKER=ref.worker_id,
            PREFIX_CHAIN_INSTANCE=ref.instance_epoch,
            PREFIX_CHAIN_NAMESPACE=uuid.uuid4().hex[:12],
        )
        # Do not pass backend control credentials into the Router process.
        env.pop("PREFIX_ROUTING_TERMINAL_TOKEN", None)
        pending = None
        buffer = bytearray()
        transcript = bytearray()
        total = 0
        try:
            process = subprocess.Popen(
                [str(binary), origin],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
            os.set_blocking(process.stdout.fileno(), False)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    connection = None
                if connection is not None:
                    with connection:
                        try:
                            message = read_frame(connection)
                            response = receiver.handle(message)
                            if "at_ns" in message:
                                last_ns = max(last_ns, message["at_ns"])
                            elif "event" in message:
                                last_ns = max(last_ns, message["event"]["at_ns"])
                            write_frame(connection, response)
                        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                            # Ambiguous control delivery never opens new admission.
                            admission_open = False
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except BlockingIOError:
                    chunk = b""
                total += len(chunk)
                if total > 1_048_576:
                    raise ValueError("Router output budget exceeded")
                buffer.extend(chunk)
                transcript.extend(chunk)
                while b"\n" in buffer:
                    line, _, tail = buffer.partition(b"\n")
                    buffer[:] = tail
                    if not line.startswith(b"{"):
                        continue
                    stage = json.loads(line)
                    if "chain_stage" not in stage:
                        continue
                    name = stage["chain_stage"]
                    if pending is not None:
                        raise ValueError(
                            "Router advanced without owner acknowledgement"
                        )
                    stages.append(stage)
                    if name in ("completion", "cancel"):
                        attempt = stage["attempt_id"]
                        if (
                            stage["quota_rejected"] is not True
                            or store.outstanding(ref) != 1
                        ):
                            raise ValueError(
                                "HTTP outcome incorrectly released admission"
                            )
                        source.outcomes[attempt] = (
                            EventKind.COMPLETED
                            if name == "completion"
                            else EventKind.CANCEL_CONFIRMED
                        )
                        pending = attempt
                    elif name == "arm_ack_loss":
                        lost_ack = True
                        pending = "barrier"
                    elif name == "ack_loss":
                        if admission_open or store.outstanding(ref) != 1:
                            raise ValueError("Lost permit did not retain debt/fence")
                        pending = "barrier"
                    elif name != "done":
                        raise ValueError("Unknown Router experiment stage")
                if not source.failed:
                    try:
                        source.consume(
                            store, ReadContext(clock, last_ns, 300_000_000_000)
                        )
                    except Exception:
                        source.failed = True
                        admission_open = False
                        raise
                if pending == "barrier" or pending in source.families:
                    process.stdin.write(b"continue\n")
                    process.stdin.flush()
                    pending = None
                if process.poll() is not None and not chunk:
                    break
            else:
                raise TimeoutError("Finite Router chain deadline exceeded")
            if process.returncode != 0:
                # Avoid persisting raw backend payloads or control credentials.
                raise RuntimeError(
                    f"Router example exit={process.returncode}; stages={len(stages)}"
                )
            if [s["chain_stage"] for s in stages] != [
                "completion",
                "cancel",
                "arm_ack_loss",
                "ack_loss",
                "done",
            ]:
                raise ValueError("Incomplete Router chain evidence")
            if (
                source.cursor != 2
                or len(source.families) != 2
                or store.outstanding(ref) != 1
            ):
                raise ValueError("Unexpected combined ledger result")
            report.update(
                terminal_count=source.cursor,
                receipt_operations=store.operation_count,
                router_pid=process.pid,
                actual_router_library_verified=True,
                http_outcome_retains_debt=True,
                post_commit_lost_permit_retains_debt=True,
                health_and_terminal_binding_checked=True,
            )
        finally:
            # Bounded generated diagnostics; never persist either capability.
            (output / "router.log").write_text(
                transcript.decode("utf-8", errors="replace")
                .replace(token, "<redacted>")
                .replace(terminal_token, "<redacted>"),
                encoding="utf-8",
            )
            if process is not None:
                report["router_cleanup"] = {"returncode": process.poll()}
                if process.poll() is None:
                    stop_owned_group(process)
                process.stdin.close()
                process.stdout.close()
    with DurableLedger(path) as recovered:
        if recovered.admission_ready or recovered.outstanding(ref) != 1:
            raise ValueError("Reopening lost or authorized the unresolved reservation")
        report["reopen_retains_debt_and_pauses"] = True
    report["status"] = "pass"
    return report
