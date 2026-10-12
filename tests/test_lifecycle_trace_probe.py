from copy import deepcopy

import pytest

from prefix_routing_mod.contracts import LifecycleRejected
from tools.lifecycle_trace_probe import validate_transcript


def transcript(sent=True, reason="transport_eof"):
    event = {
        "event_id": "attempt-a:1",
        "request_id": "request-a",
        "attempt_id": "attempt-a",
        "worker": {"worker_id": "replica-a", "instance_epoch": "service-a"},
        "sequence": 1,
        "kind": "send_started" if sent else "not_sent",
        "confirmation": "none" if sent else "not_accepted",
        "clock_id": "router-session-a",
        "at_ns": 1,
        "error_class": None,
    }
    events = [event]
    if sent:
        closed = deepcopy(event)
        closed.update(
            event_id="attempt-a:2",
            sequence=2,
            at_ns=2,
            kind="outcome_unknown",
            error_class=reason,
        )
        events.append(closed)
    return {"lifecycle_schema": "component-v0.1", "traces": [events]}


@pytest.mark.parametrize(
    "reason", ["transport_eof", "transport_error", "transport_dropped"]
)
def test_transport_endings_preserve_charge_in_existing_ledger(reason):
    result = validate_transcript(transcript(reason=reason))
    assert result["unresolved_transport_outcomes"] == 1
    assert result["backend_confirmed"] == 0
    assert result["events"] == 2


def test_definite_unsent_releases_only_its_own_charge():
    result = validate_transcript(transcript(sent=False))
    assert result["not_sent"] == 1
    assert result["unresolved_transport_outcomes"] == 0


@pytest.mark.parametrize("fault", ["gap", "identity", "confirmation", "duplicate"])
def test_malformed_native_transcript_is_not_silently_accepted(fault):
    record = transcript()
    event = record["traces"][0][-1]
    if fault == "gap":
        event["sequence"] = 3
    elif fault == "identity":
        event["worker"]["instance_epoch"] = "other-service"
    elif fault == "confirmation":
        event.update(kind="completed", confirmation="ended")
    else:
        record["traces"] *= 2
    with pytest.raises((ValueError, LifecycleRejected)):
        validate_transcript(record)


@pytest.mark.parametrize("traces", [[], [[]], [[{}] * 3], [[{}]] * 129])
def test_transcript_cardinality_is_bounded(traces):
    with pytest.raises(ValueError, match="budget"):
        validate_transcript({"lifecycle_schema": "component-v0.1", "traces": traces})
