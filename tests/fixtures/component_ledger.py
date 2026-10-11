"""Bounded, locked protocol oracle. NOT a production worker/load component."""

from dataclasses import dataclass, field, replace
from threading import RLock

from prefix_routing_mod.contracts import (
    BackendConfirmation,
    DispatchContext,
    EventKind,
    LifecycleEvent,
    LifecycleRejected,
    ReadContext,
    RequestFeatures,
    ReservationRejected,
    RouteDecision,
    WorkerRef,
    WorkerState,
)

MAX_ATTEMPT_EVENTS = 8  # Lifecycle transitions only, never per-token events.


@dataclass
class Attempt:
    decision: RouteDecision
    created_at_ns: int
    state: str = "reserved"
    charged: bool = True
    events: list[LifecycleEvent] = field(default_factory=list)


class ReferenceLedger:
    def __init__(
        self,
        workers: tuple[WorkerState, ...],
        capacity: int = 64,
        per_worker_limit: int = 64,
        config_version: str = "config-1",
    ):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be positive")
        if len({worker.ref.worker_id for worker in workers}) != len(workers):
            raise ValueError("duplicate worker ids")
        if type(per_worker_limit) is not int or per_worker_limit <= 0:
            raise ValueError("per_worker_limit must be positive")
        DispatchContext("validate", "validate", config_version)
        self._workers = {worker.ref.worker_id: worker for worker in workers}
        self._attempts: dict[str, Attempt] = {}
        self._capacity = capacity
        self._per_worker_limit = per_worker_limit
        self._config_version = config_version
        self._lock = RLock()

    def snapshot(self) -> tuple[WorkerState, ...]:
        with self._lock:
            return tuple(self._workers.values())

    def update_worker(self, worker: WorkerState) -> None:
        with self._lock:
            previous = self._workers.get(worker.ref.worker_id)
            if previous is not None and worker.version <= previous.version:
                raise ValueError("worker version must advance, even across restart")
            self._workers[worker.ref.worker_id] = worker
            self._refresh_count(worker.ref, worker.at_ns)

    def update_config(self, config_version: str) -> None:
        DispatchContext("validate", "validate", config_version)
        with self._lock:
            self._config_version = config_version

    def reserve(
        self, features: RequestFeatures, decision: RouteDecision, context: ReadContext
    ) -> None:
        with self._lock:
            worker = self._workers.get(decision.worker.worker_id)
            if (
                worker is None
                or worker.ref != decision.worker
                or worker.version != decision.worker_version
                or not worker.eligible_for(features.domain, context)
                or decision.request_id != features.request_id
                or decision.config_version != self._config_version
            ):
                raise ReservationRejected("stale, ineligible or mismatched decision")
            if decision.attempt_id in self._attempts:
                raise ReservationRejected("attempt already reserved; do not send again")
            if any(
                attempt.charged and attempt.decision.request_id == features.request_id
                for attempt in self._attempts.values()
            ):
                raise ReservationRejected("request already has unresolved work")
            if len(self._attempts) >= self._capacity:
                raise ReservationRejected("bounded attempt retention full")
            if self.outstanding(worker.ref) >= self._per_worker_limit:
                raise ReservationRejected("worker admission limit reached")
            self._attempts[decision.attempt_id] = Attempt(decision, context.now_ns)
            self._refresh_count(worker.ref, context.now_ns)

    def _refresh_count(self, ref: WorkerRef, at_ns: int) -> None:
        worker = self._workers.get(ref.worker_id)
        if worker is None or worker.ref != ref:
            return  # A late old-instance result cannot alter new-instance counts.
        value = sum(
            attempt.charged and attempt.decision.worker == ref
            for attempt in self._attempts.values()
        )
        self._workers[ref.worker_id] = replace(
            worker,
            local_outstanding=replace(
                worker.local_outstanding, value=value, at_ns=at_ns
            ),
        )

    def apply(self, event: LifecycleEvent) -> str:
        with self._lock:
            attempt = self._attempts.get(event.attempt_id)
            if attempt is None:
                raise LifecycleRejected("unknown attempt; cannot invent a reservation")
            decision = attempt.decision
            if (
                event.worker != decision.worker
                or event.request_id != decision.request_id
            ):
                raise LifecycleRejected("event identity mismatch")
            worker = self._workers.get(event.worker.worker_id)
            if worker is None or worker.ref != event.worker:
                return "stale"  # Old work requires explicit instance reconciliation.
            for previous in attempt.events:
                if previous.event_id == event.event_id:
                    if previous == event:
                        return "duplicate"
                    raise LifecycleRejected("event id reused with different payload")
            if (
                event.sequence != len(attempt.events) + 1
                or len(attempt.events) >= MAX_ATTEMPT_EVENTS
                or event.clock_id != worker.clock_id
                or event.at_ns < attempt.created_at_ns
                or (attempt.events and event.at_ns < attempt.events[-1].at_ns)
            ):
                raise LifecycleRejected("event sequence, clock or bound violation")
            state = attempt.state
            if event.confirmation is BackendConfirmation.NOT_ACCEPTED and any(
                previous.kind in (EventKind.ACCEPTED, EventKind.STREAMING)
                for previous in attempt.events
            ):
                raise LifecycleRejected(
                    "acceptance evidence contradicts non-acceptance"
                )
            target: str | None = None
            if event.kind is EventKind.SEND_STARTED and state == "reserved":
                target = "sending"
            elif event.kind is EventKind.ACCEPTED and state == "sending":
                target = "accepted"
            elif event.kind is EventKind.STREAMING and state in ("sending", "accepted"):
                target = "streaming"
            elif event.kind is EventKind.NOT_SENT and state == "reserved":
                target = "released"
            elif event.kind is EventKind.OUTCOME_UNKNOWN and state in (
                "sending",
                "accepted",
                "streaming",
            ):
                target = "uncertain"
            elif event.kind in (
                EventKind.COMPLETED,
                EventKind.CANCEL_CONFIRMED,
                EventKind.FAILED,
            ) and state in ("sending", "accepted", "streaming", "uncertain"):
                if (
                    state == "uncertain"
                    and event.kind is EventKind.FAILED
                    and event.confirmation is BackendConfirmation.NONE
                ):
                    # M5 sequences state transitions, not repeated error reports.
                    # Reject before consuming the sequence or retention budget;
                    # the next reliable confirmation must remain admissible.
                    raise LifecycleRejected(
                        "uncertain notification is not a transition"
                    )
                target = (
                    "released"
                    if event.confirmation is not BackendConfirmation.NONE
                    else "uncertain"
                )
            if target is None:
                raise LifecycleRejected("illegal lifecycle transition")
            attempt.events.append(event)
            attempt.state = target
            attempt.charged = target != "released"
            self._refresh_count(event.worker, event.at_ns)
            return "applied"

    def outstanding(self, ref: WorkerRef) -> int:
        with self._lock:
            return sum(
                attempt.charged and attempt.decision.worker == ref
                for attempt in self._attempts.values()
            )

    def uncertain(self, attempt_id: str) -> bool:
        with self._lock:
            return self._attempts[attempt_id].state == "uncertain"

    def reconcile_stopped_instance(self, ref: WorkerRef) -> None:
        """Fixture for authoritative M7 termination evidence, not a timeout."""
        with self._lock:
            worker = self._workers.get(ref.worker_id)
            if worker is not None and worker.ref == ref:
                raise LifecycleRejected("cannot retire the current service instance")
            for attempt in self._attempts.values():
                if attempt.decision.worker == ref:
                    attempt.state = "released"
                    attempt.charged = False
