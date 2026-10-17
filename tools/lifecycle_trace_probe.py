"""Replay bounded native callback transcripts into the component-v0.1 oracle.

CPU evidence only: reservations/worker identity are synthetic, and delivery is
post-run. This does not enable a production ledger or backend confirmation.
"""

import json
from pathlib import Path

from prefix_routing_mod.contracts import (
    CONTRACT_VERSION,
    BackendConfirmation,
    EventKind,
    Health,
    LifecycleEvent,
    Observation,
    Precision,
    ReadContext,
    RequestFeatures,
    RouteDecision,
    WorkerRef,
    WorkerState,
)
from tests.fixtures.component_ledger import ReferenceLedger

MAX_LOG_BYTES = 1_048_576
MAX_TRACES = 128


def validate_transcript(record: dict) -> dict:
    if record.get("lifecycle_schema") != CONTRACT_VERSION:
        raise ValueError("unsupported lifecycle transcript")
    traces = record.get("traces")
    if not isinstance(traces, list) or not 1 <= len(traces) <= MAX_TRACES:
        raise ValueError("lifecycle transcript budget")
    attempts = {}
    workers = {}
    for trace in traces:
        if not isinstance(trace, list) or not 1 <= len(trace) <= 2:
            raise ValueError("per-attempt lifecycle transcript budget")
        events = []
        for item in trace:
            fields = dict(item)
            fields["worker"] = WorkerRef(**fields["worker"])
            fields["kind"] = EventKind(fields["kind"])
            fields["confirmation"] = BackendConfirmation(fields["confirmation"])
            event = LifecycleEvent(**fields)
            if event.kind not in (
                EventKind.SEND_STARTED,
                EventKind.NOT_SENT,
                EventKind.OUTCOME_UNKNOWN,
            ):
                raise ValueError("transport callback cannot confirm backend end")
            if event.error_class not in (
                None,
                "transport_eof",
                "transport_error",
                "transport_dropped",
            ):
                raise ValueError("unsupported transport reason")
            events.append(event)
        first = events[0]
        if first.attempt_id in attempts:
            raise ValueError("attempt id reused by transcript")
        if any(
            (value.attempt_id, value.request_id, value.worker, value.clock_id)
            != (first.attempt_id, first.request_id, first.worker, first.clock_id)
            for value in events
        ):
            raise ValueError("lifecycle identity mismatch")
        if first.worker in workers and workers[first.worker] != first.clock_id:
            raise ValueError("inconsistent Router clock")
        workers[first.worker] = first.clock_id
        if [event.kind for event in events] not in (
            [EventKind.NOT_SENT],
            [EventKind.SEND_STARTED, EventKind.OUTCOME_UNKNOWN],
        ):
            raise ValueError("incomplete transport lifecycle")
        attempts[first.attempt_id] = events

    states = tuple(
        WorkerState(
            ref,
            "fixture-domain",
            0,
            Health.HEALTHY,
            False,
            clock,
            0,
            Observation(None, "fixture", "whole_instance", clock, 0),
            Observation(0, "local_ledger", "router_session", clock, 0),
        )
        for ref, clock in workers.items()
    )
    ledger = ReferenceLedger(states, capacity=MAX_TRACES, per_worker_limit=MAX_TRACES)
    # Synthetic pre-reservations allow replay of the native transcript. They do
    # not establish that the real backend accepted these requests.
    for attempt_id, events in attempts.items():
        first = events[0]
        ledger.reserve(
            RequestFeatures(
                first.request_id,
                "fixture-domain",
                None,
                None,
                None,
                None,
                Precision.UNKNOWN,
            ),
            RouteDecision(
                attempt_id,
                first.request_id,
                attempt_id,
                first.worker,
                0,
                "fixture-policy",
                "config-1",
                "fixture-only",
            ),
            ReadContext(first.clock_id, 0, 1),
        )
    ordered = sorted(
        (event for events in attempts.values() for event in events),
        key=lambda event: (event.at_ns, event.attempt_id, event.sequence),
    )
    for event in ordered:
        if ledger.apply(event) != "applied" or ledger.apply(event) != "duplicate":
            raise ValueError("lifecycle replay or deduplication failed")
    unknown = sum(ledger.uncertain(attempt_id) for attempt_id in attempts)
    outstanding = sum(ledger.outstanding(ref) for ref in workers)
    if outstanding != unknown:
        raise ValueError("transport replay lost unresolved debt")
    return {
        "schema": CONTRACT_VERSION,
        "attempts": len(attempts),
        "events": len(ordered),
        "unresolved_transport_outcomes": unknown,
        "not_sent": len(attempts) - unknown,
        "backend_confirmed": 0,
    }


def validate_log(path: Path) -> dict:
    with path.open("rb") as stream:
        payload = stream.read(MAX_LOG_BYTES + 1)
    if len(payload) > MAX_LOG_BYTES:
        raise ValueError("lifecycle log budget")
    records = [
        json.loads(line)
        for line in payload.decode().splitlines()
        if line.startswith('{"lifecycle_schema":')
    ]
    if len(records) != 1:
        raise ValueError("missing or duplicate lifecycle transcript")
    return validate_transcript(records[0])
