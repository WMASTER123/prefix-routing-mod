"""CPU transaction/restart proof around the existing reference ledger.

SQLite is an experimental receipt log, not the selected production M3 store.
Only reservation/lifecycle metadata is retained, never prompts or token IDs.
The reference clock is replayed for historical validation; admission remains
paused after reopening. Explicit CPU handover requires fresh observations and
operator confirmation that the old dispatcher is stopped; not distributed HA.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock

from prefix_routing_mod.contracts import (
    BackendConfirmation,
    EventKind,
    Health,
    LifecycleEvent,
    LifecycleRejected,
    Observation,
    Precision,
    ReadContext,
    RequestFeatures,
    ReservationRejected,
    RouteDecision,
    WorkerRef,
    WorkerState,
)
from tests.fixtures.component_ledger import ReferenceLedger
from tools.terminal_observer_prototype import Binding, Ended

MAX_RECORD_BYTES = 8192
MAX_OPERATIONS = 100_000
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = item
        return result

    if not isinstance(value, str) or len(value.encode()) > MAX_RECORD_BYTES:
        raise ValueError("receipt exceeds record budget")
    return json.loads(value, object_pairs_hook=unique)


def worker_from(value):
    value = dict(value)
    value["ref"] = WorkerRef(**value["ref"])
    value["health"] = Health(value["health"])
    for field in ("backend_running", "local_outstanding"):
        value[field] = Observation(**value[field])
    return WorkerState(**value)


def decision_from(value):
    value = dict(value)
    value["worker"] = WorkerRef(**value["worker"])
    return RouteDecision(**value)


def event_from(value):
    value = dict(value)
    value["worker"] = WorkerRef(**value["worker"])
    value["kind"] = EventKind(value["kind"])
    value["confirmation"] = BackendConfirmation(value["confirmation"])
    return LifecycleEvent(**value)


class DurableLedger:
    """Single serialized CPU owner; SQLite commit is the receipt boundary.

    The same oracle is mutated and reconstructed; no second state machine.
    Retention has no GC. Faults poison the current instance; reopening replays
    committed operations but never resumes dispatch automatically.
    """

    def __init__(
        self,
        path: Path,
        *,
        create=False,
        workers=(),
        capacity=64,
        per_worker_limit=64,
        config_version="config-1",
        max_operations=512,
        max_payload_bytes=2 * 1024 * 1024,
        fault=None,
    ):
        self.path = path.resolve()
        self.lock = RLock()
        self.fault = fault or (lambda point: None)
        self.failed = False
        self.admission_ready = False
        if create:
            for bound in (
                max_operations,
                max_payload_bytes,
                capacity,
                per_worker_limit,
            ):
                if type(bound) is not int or bound <= 0:
                    raise ValueError("positive integer bounds required")
            if max_operations > MAX_OPERATIONS or max_payload_bytes > MAX_PAYLOAD_BYTES:
                raise ValueError("absolute receipt budget exceeded")
            if not isinstance(workers, tuple) or not workers:
                raise ValueError("explicit worker tuple required")
            if any(worker.local_outstanding.value != 0 for worker in workers):
                raise ValueError("bootstrap cannot invent existing reservations")
            # Validate the oracle and metadata before touching the output path.
            ReferenceLedger(workers, capacity, per_worker_limit, config_version)
            metadata = encode(
                {
                    "schema": 1,
                    "workers": [asdict(worker) for worker in workers],
                    "capacity": capacity,
                    "per_worker_limit": per_worker_limit,
                    "config_version": config_version,
                    "max_operations": max_operations,
                    "max_payload_bytes": max_payload_bytes,
                }
            )
            decode(metadata)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("xb"):
                pass
        elif not self.path.is_file():
            raise FileNotFoundError("receipt log missing; cannot assume no prior work")
        if self.path.stat().st_size > 2 * MAX_PAYLOAD_BYTES:
            raise ValueError("receipt file exceeds open budget")
        self.connection = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None
        )
        try:
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            if create:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "CREATE TABLE bootstrap (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "payload TEXT NOT NULL)"
                )
                self.connection.execute(
                    "CREATE TABLE receipts (seq INTEGER PRIMARY KEY, "
                    "payload TEXT NOT NULL)"
                )
                self.connection.execute(
                    "INSERT INTO bootstrap VALUES(1,?)", (metadata,)
                )
                self.connection.execute("COMMIT")
            self._reload()
            self.admission_ready = create
        except BaseException:
            self.connection.close()
            raise

    def _reload(self):
        if self.connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("receipt log integrity failure")
        rows = self.connection.execute("SELECT id,payload FROM bootstrap").fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise ValueError("invalid bootstrap")
        meta = decode(rows[0][1])
        required = {
            "schema",
            "workers",
            "capacity",
            "per_worker_limit",
            "config_version",
            "max_operations",
            "max_payload_bytes",
        }
        if (
            not isinstance(meta, dict)
            or set(meta) != required
            or type(meta["schema"]) is not int
            or meta["schema"] != 1
        ):
            raise ValueError("unknown receipt schema")
        for name in (
            "capacity",
            "per_worker_limit",
            "max_operations",
            "max_payload_bytes",
        ):
            if type(meta[name]) is not int or meta[name] <= 0:
                raise ValueError("invalid persisted bound")
        self.max_operations = meta["max_operations"]
        self.max_payload_bytes = meta["max_payload_bytes"]
        if (
            self.max_operations > MAX_OPERATIONS
            or self.max_payload_bytes > MAX_PAYLOAD_BYTES
        ):
            raise ValueError("persisted absolute receipt budget exceeded")
        count, size = self.connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) "
            "FROM receipts"
        ).fetchone()
        if count > self.max_operations or size > self.max_payload_bytes:
            raise ValueError("persisted receipt budget exceeded")
        workers = tuple(worker_from(worker) for worker in meta["workers"])
        if any(worker.local_outstanding.value != 0 for worker in workers):
            raise ValueError("bootstrap has untracked reservations")
        oracle = ReferenceLedger(
            workers,
            meta["capacity"],
            meta["per_worker_limit"],
            meta["config_version"],
        )
        terminal_receipts = {}
        transport_receipts = {}
        for expected, (sequence, payload) in enumerate(
            self.connection.execute("SELECT seq,payload FROM receipts ORDER BY seq"), 1
        ):
            if sequence != expected:
                raise ValueError("receipt sequence gap")
            operation = decode(payload)
            if self._apply_to(oracle, operation) != "applied":
                raise ValueError("invalid/repeated committed operation")
            if operation["kind"] == "terminal":
                record = ended_from(operation["source"])
                key = terminal_key(record)
                if key in terminal_receipts:
                    raise ValueError("duplicate observer receipt")
                terminal_receipts[key] = operation
            if operation["kind"] == "transport":
                source = event_from(operation["source"])
                key = transport_key(source)
                if key in transport_receipts:
                    raise ValueError("duplicate transport receipt")
                validate_transport_order(source, transport_receipts)
                transport_receipts[key] = operation
        self.oracle = oracle
        self.terminal_receipts = terminal_receipts
        self.transport_receipts = transport_receipts
        self.operation_count = count
        self.payload_bytes = size

    @staticmethod
    def _apply_to(oracle, operation):
        if not isinstance(operation, dict):
            raise ValueError("operation must be an object")
        if operation.get("kind") == "runtime":
            return apply_runtime(oracle, operation)
        if operation.get("kind") == "reserve":
            if set(operation) != {
                "kind",
                "request_id",
                "domain",
                "decision",
                "context",
            }:
                raise ValueError("invalid reservation receipt")
            features = RequestFeatures(
                operation["request_id"],
                operation["domain"],
                None,
                None,
                None,
                None,
                Precision.UNKNOWN,
            )
            oracle.reserve(
                features,
                decision_from(operation["decision"]),
                ReadContext(**operation["context"]),
            )
            return "applied"
        if operation.get("kind") == "event" and set(operation) == {"kind", "event"}:
            return oracle.apply(event_from(operation["event"]))
        if operation.get("kind") == "transport" and set(operation) == {
            "kind",
            "source",
            "event",
        }:
            source = event_from(operation["source"])
            generated = transport_transition(oracle, source)
            if operation["event"] != (asdict(generated) if generated else None):
                raise ValueError("transport transition differs from stored receipt")
            if generated is not None and oracle.apply(generated) != "applied":
                raise LifecycleRejected("transport transition was not applied")
            return "applied"  # The observation itself is durably acknowledged.
        if operation.get("kind") == "terminal" and set(operation) == {
            "kind",
            "event",
            "source",
        }:
            record = ended_from(operation["source"])
            event = event_from(operation["event"])
            if (
                event.worker
                != WorkerRef(record.binding.worker_id, record.binding.instance_epoch)
                or event.attempt_id != record.attempt_id
                or event.confirmation is not BackendConfirmation.ENDED
            ):
                raise ValueError("terminal receipt identity mismatch")
            return oracle.apply(event)
        raise ValueError("invalid receipt operation")

    def reserve(self, features, decision, context):
        with self.lock:
            if not self.admission_ready:
                raise ReservationRejected("restart requires fresh runtime binding")
            return self._receive(
                {
                    "kind": "reserve",
                    "request_id": features.request_id,
                    "domain": features.domain,
                    "decision": asdict(decision),
                    "context": asdict(context),
                }
            )

    def apply(self, event):
        with self.lock:
            if event.kind is EventKind.SEND_STARTED:
                self._check_send_runtime(event.attempt_id, event.clock_id)
            return self._receive({"kind": "event", "event": asdict(event)})

    def resume(self, workers, context, *, previous_dispatcher_stopped=False):
        """Explicit CPU takeover. Caller must establish exclusive dispatch first.

        No health/network/OS evidence is obtained here. Same service instances
        only; old unknown/reserved work remains charged, old permits are fenced.
        """
        with self.lock:
            if previous_dispatcher_stopped is not True or not isinstance(
                workers, tuple
            ):
                raise ReservationRejected(
                    "exclusive dispatcher stop confirmation required"
                )
            if self.admission_ready:
                raise ReservationRejected("pause/reopen before runtime takeover")
            result = self._receive(
                {
                    "kind": "runtime",
                    "workers": [asdict(worker) for worker in workers],
                    "context": asdict(context),
                }
            )
            self.admission_ready = True
            return result

    def _check_send_runtime(self, attempt_id, clock_id):
        attempt = self.oracle._attempts.get(attempt_id)
        worker = (
            self.oracle._workers.get(attempt.decision.worker.worker_id)
            if attempt
            else None
        )
        if (
            not self.admission_ready
            or self.failed
            or attempt is None
            or worker is None
            or worker.ref != attempt.decision.worker
            or clock_id != worker.clock_id
            or clock_id != attempt.clock_id
        ):
            raise ReservationRejected("restart cannot authorize an old or unknown send")

    def receive_batch(self, operations):
        """Bounded group-commit experiment; all successes return AFTER commit.

        Operations are internally generated metadata, not a network/JSON API.
        No batching timer or background thread is introduced.
        """
        with self.lock:
            if not isinstance(operations, tuple) or not 0 < len(operations) <= 64:
                raise ValueError("bounded nonempty operation tuple required")
            for operation in operations:
                if not isinstance(operation, dict) or operation.get("kind") not in (
                    "reserve",
                    "event",
                ):
                    raise ValueError(
                        "only internal reservation/event batches supported"
                    )
                if not self.admission_ready and (
                    operation["kind"] == "reserve"
                    or event_from(operation["event"]).kind is EventKind.SEND_STARTED
                ):
                    raise ReservationRejected("restart cannot authorize new work")
                if (
                    operation["kind"] == "event"
                    and event_from(operation["event"]).kind is EventKind.SEND_STARTED
                ):
                    # A preceding reservation in THIS atomic batch is checked
                    # by the oracle; preexisting attempts must be current-runtime.
                    record = event_from(operation["event"])
                    if record.attempt_id in self.oracle._attempts:
                        self._check_send_runtime(record.attempt_id, record.clock_id)
            return self._receive_many(operations)

    def notify_transport(self, attempt_id, kind, context):
        """Collapse repeated/late callbacks; only applied SEND_STARTED permits send."""
        with self.lock:
            if self.failed:
                raise RuntimeError("receipt owner failed; reopen for recovery")
            if kind not in (EventKind.SEND_STARTED, EventKind.OUTCOME_UNKNOWN):
                raise ValueError("unsupported transport callback")
            attempt = self.oracle._attempts.get(attempt_id)
            if attempt is None:
                raise LifecycleRejected("unknown transport attempt")
            if (
                attempt.state == "released"
                or (kind is EventKind.OUTCOME_UNKNOWN and attempt.state == "uncertain")
                or (kind is EventKind.SEND_STARTED and attempt.state != "reserved")
            ):
                return "no_transition"
            if kind is EventKind.SEND_STARTED:
                self._check_send_runtime(attempt_id, context.clock_id)
            sequence = len(attempt.events) + 1
            decision = attempt.decision
            generated = LifecycleEvent(
                hashlib.sha256(f"{attempt_id}:{kind}:{sequence}".encode()).hexdigest(),
                decision.request_id,
                attempt_id,
                decision.worker,
                sequence,
                kind,
                BackendConfirmation.NONE,
                context.clock_id,
                context.now_ns,
            )
            return self._receive({"kind": "event", "event": asdict(generated)})

    def receive_transport(self, source):
        """Receive old or live observations; NEVER authorize a new HTTP send.

        The source sequence belongs to the callback stream. Only this owner
        assigns ledger sequence numbers; terminal evidence may interleave.
        """
        with self.lock:
            if self.failed:
                raise RuntimeError("receipt owner failed; reopen for recovery")
            validate_transport(source)
            key = transport_key(source)
            prior = self.transport_receipts.get(key)
            if prior is not None:
                if prior["source"] != asdict(source):
                    raise LifecycleRejected("transport source reused with new payload")
                return "duplicate"
            validate_transport_order(source, self.transport_receipts)
            generated = transport_transition(self.oracle, source)
            operation = {
                "kind": "transport",
                "source": asdict(source),
                "event": asdict(generated) if generated else None,
            }
            result = self._receive(operation)
            self.transport_receipts[key] = operation
            return result

    def confirm_end(self, record, kind, context, *, error_class=None):
        """One owner allocates lifecycle sequence; repeat uses committed payload.

        The caller supplies independently known outcome, not inferred from Ended.
        Oracle internals are used only in this experiment; no public ABI change.
        """
        with self.lock:
            if self.failed:
                raise RuntimeError("receipt owner failed; reopen for recovery")
            if not isinstance(record, Ended) or kind not in (
                EventKind.COMPLETED,
                EventKind.CANCEL_CONFIRMED,
                EventKind.FAILED,
            ):
                raise ValueError("known terminal outcome required")
            key = terminal_key(record)
            prior = self.terminal_receipts.get(key)
            if prior is not None:
                prior_event = event_from(prior["event"])
                if (
                    prior["source"] != asdict(record)
                    or prior_event.kind != kind
                    or prior_event.error_class != error_class
                ):
                    raise LifecycleRejected(
                        "observer receipt reused with different evidence"
                    )
                return "duplicate"
            attempt = self.oracle._attempts.get(record.attempt_id)
            if attempt is None:
                raise LifecycleRejected("unknown observer attempt")
            sequence = len(attempt.events) + 1
            decision = attempt.decision
            generated = LifecycleEvent(
                hashlib.sha256(encode(asdict(record)).encode()).hexdigest(),
                decision.request_id,
                record.attempt_id,
                WorkerRef(record.binding.worker_id, record.binding.instance_epoch),
                sequence,
                kind,
                BackendConfirmation.ENDED,
                context.clock_id,
                context.now_ns,
                error_class,
            )
            operation = {
                "kind": "terminal",
                "event": asdict(generated),
                "source": asdict(record),
            }
            result = self._receive(operation)
            self.terminal_receipts[key] = operation
            return result

    def _receive(self, operation):
        return self._receive_many((operation,))[0]

    def _receive_many(self, operations):
        if self.failed:
            raise RuntimeError("receipt owner failed; reopen for recovery")
        encoded = tuple(encode(operation) for operation in operations)
        normalized = tuple(decode(payload) for payload in encoded)
        transaction_started = False
        try:
            results, new_payloads = [], []
            for operation, payload in zip(normalized, encoded, strict=True):
                result = self._apply_to(self.oracle, operation)
                results.append(result)
                if result == "applied":
                    new_payloads.append(payload)
                elif result != "duplicate":
                    raise LifecycleRejected("stale evidence cannot be acknowledged")
            if not new_payloads:
                return tuple(results)
            added_bytes = sum(len(payload.encode()) for payload in new_payloads)
            if (
                self.operation_count + len(new_payloads) > self.max_operations
                or self.payload_bytes + added_bytes > self.max_payload_bytes
            ):
                raise RuntimeError("receipt retention full")
            transaction_started = True
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.executemany(
                "INSERT INTO receipts VALUES(?,?)",
                [
                    (self.operation_count + offset, payload)
                    for offset, payload in enumerate(new_payloads, 1)
                ],
            )
            self.fault("before_commit")
            self.connection.execute("COMMIT")
            self.operation_count += len(new_payloads)
            self.payload_bytes += added_bytes
            self.fault("after_commit_before_ack")
            return tuple(results)
        except BaseException as error:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            # Full capacity/stale decisions are normal admission rejections,
            # not an observation-channel failure. Undo speculative mutations
            # without disabling receipt processing for already-running work.
            if not isinstance(error, ReservationRejected) or transaction_started:
                self.failed = True
                self.admission_ready = False
            self._reload()
            raise

    def outstanding(self, worker):
        with self.lock:
            return self.oracle.outstanding(worker)

    def close(self):
        self.failed = True
        self.admission_ready = False
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def ended_from(value):
    value = dict(value)
    value["binding"] = Binding(**value["binding"])
    return Ended(**value)


def terminal_key(record):
    return (
        record.binding.worker_id,
        record.binding.instance_epoch,
        record.binding.observer_epoch,
        record.sequence,
    )


def transport_key(source):
    return (source.clock_id, source.attempt_id, source.sequence)


def validate_transport(source):
    if not isinstance(source, LifecycleEvent):
        raise ValueError("transport observation must be a lifecycle record")
    expected = {
        EventKind.SEND_STARTED: (1, BackendConfirmation.NONE, (None,)),
        EventKind.NOT_SENT: (1, BackendConfirmation.NOT_ACCEPTED, (None,)),
        EventKind.OUTCOME_UNKNOWN: (
            2,
            BackendConfirmation.NONE,
            ("transport_eof", "transport_error", "transport_dropped"),
        ),
    }.get(source.kind)
    if expected is None or (
        source.sequence != expected[0]
        or source.confirmation != expected[1]
        or source.error_class not in expected[2]
    ):
        raise LifecycleRejected("invalid transport observation")


def validate_transport_order(source, receipts):
    validate_transport(source)
    if source.sequence == 2:
        previous = receipts.get((source.clock_id, source.attempt_id, 1))
        if previous is None:
            raise LifecycleRejected("transport source gap")
        first = event_from(previous["source"])
        if (
            first.kind is not EventKind.SEND_STARTED
            or first.request_id != source.request_id
            or first.worker != source.worker
            or first.at_ns > source.at_ns
        ):
            raise LifecycleRejected("transport source order/identity mismatch")


def transport_transition(oracle, source):
    validate_transport(source)
    attempt = oracle._attempts.get(source.attempt_id)
    if attempt is None:
        raise LifecycleRejected("transport cannot invent a reservation")
    decision = attempt.decision
    worker = oracle._workers.get(decision.worker.worker_id)
    if (
        source.request_id != decision.request_id
        or source.worker != decision.worker
        or worker is None
        or worker.ref != source.worker
        or source.clock_id != attempt.clock_id
        or source.at_ns < attempt.created_at_ns
    ):
        raise LifecycleRejected("transport binding mismatch")
    if attempt.state == "released" or (
        source.kind is EventKind.OUTCOME_UNKNOWN and attempt.state == "uncertain"
    ):
        if source.kind is not EventKind.OUTCOME_UNKNOWN:
            raise LifecycleRejected("conflicting late send/unsent observation")
        return None
    return replace(
        source,
        event_id=hashlib.sha256(encode(asdict(source)).encode()).hexdigest(),
        sequence=len(attempt.events) + 1,
    )


def apply_runtime(oracle, operation):
    if set(operation) != {"kind", "workers", "context"}:
        raise ValueError("invalid runtime handover")
    context = ReadContext(**operation["context"])
    if context.clock_id in oracle._runtime_clocks:
        raise ReservationRejected("runtime clock must never be reused")
    workers = tuple(worker_from(value) for value in operation["workers"])
    if len(workers) != len(oracle._workers) or len(
        {worker.ref.worker_id for worker in workers}
    ) != len(workers):
        raise ReservationRejected("complete existing worker observations required")
    for worker in workers:
        previous = oracle._workers.get(worker.ref.worker_id)
        if (
            previous is None
            or worker.ref != previous.ref
            or worker.domain != previous.domain
            or worker.version <= previous.version
            or worker.clock_id != context.clock_id
            or not worker.eligible_for(previous.domain, context)
            or worker.backend_running.clock_id != context.clock_id
            or not context.fresh(context.clock_id, worker.backend_running.at_ns)
            or not context.fresh(context.clock_id, worker.local_outstanding.at_ns)
            or worker.local_outstanding.value != oracle.outstanding(worker.ref)
        ):
            raise ReservationRejected(
                "fresh same-instance observation and retained debt required"
            )
    for worker in workers:
        oracle.update_worker(worker)
    oracle._runtime_clocks.add(context.clock_id)
    return "applied"
