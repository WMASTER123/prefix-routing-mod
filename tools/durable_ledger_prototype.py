"""CPU transaction/restart proof around the existing reference ledger.

SQLite is an experimental receipt log, not the selected production M3 store.
Only reservation/lifecycle metadata is retained, never prompts or token IDs.
The reference clock is replayed for historical validation; admission remains
paused after reopening until a production clock/observation handover exists.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict
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
        self.oracle = oracle
        self.terminal_receipts = terminal_receipts
        self.operation_count = count
        self.payload_bytes = size

    @staticmethod
    def _apply_to(oracle, operation):
        if not isinstance(operation, dict):
            raise ValueError("operation must be an object")
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
            return self._receive({"kind": "event", "event": asdict(event)})

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
            if kind is EventKind.SEND_STARTED and not self.admission_ready:
                raise ReservationRejected("restart cannot authorize a new send")
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
        if self.failed:
            raise RuntimeError("receipt owner failed; reopen for recovery")
        payload = encode(operation)
        decode(payload)
        try:
            result = self._apply_to(self.oracle, operation)
            if result == "duplicate":
                return result  # The original receipt already committed.
            if result != "applied":
                raise LifecycleRejected("stale evidence cannot be acknowledged")
            if (
                self.operation_count >= self.max_operations
                or self.payload_bytes + len(payload.encode()) > self.max_payload_bytes
            ):
                raise RuntimeError("receipt retention full")
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO receipts VALUES(?,?)",
                (
                    self.operation_count + 1,
                    payload,
                ),
            )
            self.fault("before_commit")
            self.connection.execute("COMMIT")
            self.operation_count += 1
            self.payload_bytes += len(payload.encode())
            self.fault("after_commit_before_ack")
            return result
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
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
