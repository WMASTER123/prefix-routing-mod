import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from prefix_routing_mod.contracts import (
    CONTRACT_VERSION,
    MAX_TOKEN_IDS,
    BackendConfirmation,
    CacheRef,
    EventKind,
    Health,
    LifecycleEvent,
    LifecycleRejected,
    Observation,
    Precision,
    PrefixMatch,
    ReadContext,
    RequestFeatures,
    ReservationRejected,
    ResidencySource,
    RouteDecision,
    SemanticIdentity,
    Trust,
    WorkerRef,
    WorkerState,
)
from tests.fixtures.component_ledger import ReferenceLedger

IDENTITY = SemanticIdentity("domain-a", "model", "tokens", "template", "kv")
REF = WorkerRef("replica-a", "service-1")
CONTEXT = ReadContext("router-session-1", 100, 50)
FEATURES = RequestFeatures(
    "request-a", "domain-a", IDENTITY, 3, (1, 2, 3), 2, Precision.EXACT
)


def worker(ref=REF, version=0, **changes):
    return replace(
        WorkerState(
            ref,
            "domain-a",
            version,
            Health.HEALTHY,
            False,
            CONTEXT.clock_id,
            90,
            Observation(
                None, "backend_metrics", "whole_instance", CONTEXT.clock_id, 90
            ),
            Observation(0, "local_ledger", "router_session", CONTEXT.clock_id, 90),
        ),
        **changes,
    )


def decision(state=None, attempt_id="attempt-a", request_id="request-a"):
    state = state or worker()
    return RouteDecision(
        "decision-a",
        request_id,
        attempt_id,
        state.ref,
        state.version,
        "fixture-1",
        "config-1",
        "fixture_only",
    )


def event(kind, confirmation=BackendConfirmation.NONE, sequence=1, **changes):
    return replace(
        LifecycleEvent(
            f"event-{sequence}",
            "request-a",
            "attempt-a",
            REF,
            sequence,
            kind,
            confirmation,
            CONTEXT.clock_id,
            100 + sequence,
        ),
        **changes,
    )


def reserved(capacity=64):
    ledger = ReferenceLedger((worker(),), capacity)
    ledger.reserve(FEATURES, decision(), CONTEXT)
    return ledger


@pytest.mark.parametrize("value", ["", "a b", "\n", "\x7f", "a" * 129, None, 1])
def test_identifiers_are_bounded(value):
    with pytest.raises(ValueError):
        WorkerRef(value, "instance")


@pytest.mark.parametrize(
    "changes",
    [
        {"identity": None},
        {"token_ids": [1, 2, 3]},
        {"input_tokens": None},
        {"input_tokens": True},
        {"input_tokens": 2},
        {"block_size": 0},
        {"block_size": True},
        {"token_ids": (1, -1, 3)},
        {"token_ids": (1, True, 3)},
        {"token_ids": (1, 2**32, 3)},
        {"domain": "other"},
        {"precision": "exact"},
        {"precision": Precision.ESTIMATED},
    ],
)
def test_exact_features_cannot_be_manufactured(changes):
    with pytest.raises(ValueError):
        replace(FEATURES, **changes)


def test_token_feature_limit_and_immutability():
    with pytest.raises(ValueError, match="limit"):
        replace(
            FEATURES,
            input_tokens=MAX_TOKEN_IDS + 1,
            token_ids=(0,) * (MAX_TOKEN_IDS + 1),
        )
    with pytest.raises(FrozenInstanceError):
        FEATURES.request_id = "changed"
    unknown = RequestFeatures(
        "unknown", "domain-a", None, None, None, None, Precision.UNKNOWN
    )
    assert unknown.input_tokens is None


def match(**changes):
    return replace(
        PrefixMatch(
            CacheRef(REF, "publisher-1"),
            IDENTITY,
            2,
            2,
            Trust.TRUSTED,
            CONTEXT.clock_id,
            90,
            ResidencySource.KV_EVENTS,
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"identity": replace(IDENTITY, template="different")},
        {"block_size": 1},
        {"at_ns": 49},
        {"at_ns": 101},
        {"clock_id": "remote-clock"},
        {"matched_tokens": 4},
        {"trust": Trust.UNKNOWN, "matched_tokens": None},
        {"trust": Trust.RECOVERING, "matched_tokens": None},
    ],
)
def test_match_eligibility_requires_fresh_exact_complete_evidence(changes):
    assert not match(**changes).usable_for(FEATURES, CONTEXT)


def test_unknown_residency_is_not_a_miss():
    assert match(matched_tokens=0).usable_for(FEATURES, CONTEXT)
    assert match().usable_for(FEATURES, CONTEXT)
    with pytest.raises(ValueError):
        match(trust=Trust.UNKNOWN, matched_tokens=0)
    with pytest.raises(ValueError):
        match(matched_tokens=1)


def test_epochs_are_independent():
    before = CacheRef(REF, "publisher-1")
    after = CacheRef(REF, "publisher-2")
    assert before != after and before.worker == after.worker
    ledger = reserved()
    assert ledger.outstanding(after.worker) == 1


CASES = json.loads(
    (Path(__file__).parent / "fixtures/component-v0.1.json").read_text("utf-8")
)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_versioned_lifecycle_examples(case):
    assert CASES["contract_version"] == CONTRACT_VERSION
    ledger = reserved()
    for sequence, ((kind, confirmation), expected) in enumerate(
        zip(case["events"], case["outstanding"], strict=True), 1
    ):
        notification = event(
            EventKind(kind), BackendConfirmation(confirmation), sequence
        )
        assert ledger.apply(notification) == "applied"
        assert ledger.apply(notification) == "duplicate"
        assert ledger.outstanding(REF) == expected
    assert ledger.uncertain("attempt-a") == case["uncertain"]


@pytest.mark.parametrize(
    "changes",
    [
        {"health": Health.UNKNOWN},
        {"health": Health.UNHEALTHY},
        {"draining": True},
        {"domain": "other"},
        {"at_ns": 49},
        {
            "clock_id": "backend-clock",
            "local_outstanding": Observation(
                0, "local_ledger", "router_session", "backend-clock", 90
            ),
        },
    ],
)
def test_presend_recheck_rejects_unavailable_targets(changes):
    state = worker(**changes)
    ledger = ReferenceLedger((state,))
    with pytest.raises(ReservationRejected):
        ledger.reserve(FEATURES, decision(state), CONTEXT)
    assert ledger.outstanding(REF) == 0


def test_atomic_admission_bound_under_concurrent_selections():
    ledger = ReferenceLedger((worker(),), per_worker_limit=1)

    def attempt(index):
        features = replace(FEATURES, request_id=f"request-{index}")
        selected = decision(
            attempt_id=f"attempt-{index}", request_id=features.request_id
        )
        try:
            ledger.reserve(features, selected, CONTEXT)
        except ReservationRejected:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))
    assert sum(results) == 1
    assert ledger.outstanding(REF) == 1


def test_changed_health_after_selection_rejects_reservation():
    ledger = ReferenceLedger((worker(),))
    selected = decision()
    ledger.update_worker(worker(version=1, draining=True))
    with pytest.raises(ReservationRejected):
        ledger.reserve(FEATURES, selected, CONTEXT)


def test_distinct_attempts_need_not_retry_on_every_load_update():
    ledger = ReferenceLedger((worker(),))
    first_version = ledger.snapshot()[0].version
    ledger.reserve(FEATURES, decision(), CONTEXT)
    ledger.reserve(
        replace(FEATURES, request_id="request-b"),
        decision(attempt_id="attempt-b", request_id="request-b"),
        CONTEXT,
    )
    assert ledger.outstanding(REF) == 2
    assert ledger.snapshot()[0].version == first_version


def test_config_change_between_selection_and_reservation_rejected():
    ledger = ReferenceLedger((worker(),))
    selected = decision()
    ledger.update_config("config-2")
    with pytest.raises(ReservationRejected):
        ledger.reserve(FEATURES, selected, CONTEXT)
    assert ledger.outstanding(REF) == 0


def test_request_history_is_not_an_observed_residency_source():
    with pytest.raises(ValueError):
        match(source="request_history")


def test_duplicate_reservation_never_authorizes_another_send():
    ledger = reserved()
    with pytest.raises(ReservationRejected):
        ledger.reserve(FEATURES, decision(ledger.snapshot()[0]), CONTEXT)
    assert ledger.outstanding(REF) == 1


def test_post_send_unknown_blocks_retry_until_backend_reconciliation():
    ledger = reserved()
    ledger.apply(event(EventKind.SEND_STARTED))
    ledger.apply(event(EventKind.OUTCOME_UNKNOWN, sequence=2))
    with pytest.raises(ReservationRejected, match="unresolved"):
        ledger.reserve(FEATURES, decision(ledger.snapshot()[0], "attempt-b"), CONTEXT)
    ledger.apply(event(EventKind.CANCEL_CONFIRMED, BackendConfirmation.ENDED, 3))
    ledger.reserve(FEATURES, decision(ledger.snapshot()[0], "attempt-b"), CONTEXT)
    assert ledger.outstanding(REF) == 1


def test_presend_rollback_allows_new_attempt_and_preserves_dedup():
    ledger = reserved()
    rolled_back = event(EventKind.NOT_SENT, BackendConfirmation.NOT_ACCEPTED)
    ledger.apply(rolled_back)
    ledger.reserve(FEATURES, decision(ledger.snapshot()[0], "attempt-b"), CONTEXT)
    assert ledger.apply(rolled_back) == "duplicate"
    assert ledger.outstanding(REF) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": 2},
        {"request_id": "other"},
        {"worker": WorkerRef("a", "b")},
        {"clock_id": "remote-clock"},
        {"at_ns": 99},
    ],
)
def test_lifecycle_gap_identity_or_clock_errors_do_not_release_work(changes):
    ledger = reserved()
    with pytest.raises(LifecycleRejected):
        ledger.apply(event(EventKind.SEND_STARTED, **changes))
    assert ledger.outstanding(REF) == 1


def test_illegal_terminal_and_conflicting_duplicate_rejected():
    ledger = reserved()
    with pytest.raises(LifecycleRejected):
        ledger.apply(event(EventKind.COMPLETED, BackendConfirmation.ENDED))
    started = event(EventKind.SEND_STARTED)
    ledger.apply(started)
    with pytest.raises(LifecycleRejected, match="payload"):
        ledger.apply(replace(started, at_ns=110))
    ledger.apply(event(EventKind.ACCEPTED, sequence=2))
    with pytest.raises(LifecycleRejected, match="contradicts"):
        ledger.apply(event(EventKind.FAILED, BackendConfirmation.NOT_ACCEPTED, 3))
    assert ledger.outstanding(REF) == 1


def test_client_cancel_needs_backend_confirmation():
    with pytest.raises(ValueError):
        event(EventKind.CANCEL_CONFIRMED)
    with pytest.raises(ValueError):
        event(EventKind.OUTCOME_UNKNOWN, BackendConfirmation.ENDED)


def test_uncertainty_does_not_erase_previous_acceptance_evidence():
    ledger = reserved()
    ledger.apply(event(EventKind.SEND_STARTED))
    ledger.apply(event(EventKind.ACCEPTED, sequence=2))
    ledger.apply(event(EventKind.OUTCOME_UNKNOWN, sequence=3))
    with pytest.raises(LifecycleRejected, match="contradicts"):
        ledger.apply(event(EventKind.FAILED, BackendConfirmation.NOT_ACCEPTED, 4))
    assert ledger.outstanding(REF) == 1


def test_service_restart_does_not_let_old_events_release_new_work():
    ledger = reserved()
    ledger.apply(event(EventKind.SEND_STARTED))
    new_ref = WorkerRef(REF.worker_id, "service-2")
    ledger.update_worker(worker(ref=new_ref, version=10))
    new_features = replace(FEATURES, request_id="request-b")
    ledger.reserve(
        new_features, decision(ledger.snapshot()[0], "attempt-b", "request-b"), CONTEXT
    )
    assert (
        ledger.apply(event(EventKind.COMPLETED, BackendConfirmation.ENDED, 2))
        == "stale"
    )
    assert ledger.outstanding(new_ref) == 1
    assert ledger.outstanding(REF) == 1
    ledger.reconcile_stopped_instance(REF)
    assert ledger.outstanding(REF) == 0
    assert ledger.outstanding(new_ref) == 1
    with pytest.raises(LifecycleRejected):
        ledger.reconcile_stopped_instance(new_ref)


def test_counts_have_distinct_scopes_and_update_cannot_overwrite_ledger():
    ledger = reserved()
    old = ledger.snapshot()[0]
    assert old.backend_running.value is None
    ledger.update_worker(
        replace(
            old,
            version=old.version + 1,
            local_outstanding=replace(old.local_outstanding, value=0),
        )
    )
    assert ledger.snapshot()[0].local_outstanding.value == 1


def test_backend_count_cannot_be_relabelled_as_local_ledger():
    with pytest.raises(ValueError, match="local count"):
        worker(
            local_outstanding=Observation(
                2, "backend_metrics", "whole_instance", CONTEXT.clock_id, 90
            )
        )


def test_concurrent_duplicate_attempt_has_exactly_one_reservation():
    ledger = ReferenceLedger((worker(),))

    def reserve_same(_):
        try:
            ledger.reserve(FEATURES, decision(), CONTEXT)
        except ReservationRejected:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(reserve_same, range(8))) == 1
    assert ledger.outstanding(REF) == 1


def test_retention_full_fails_closed_even_after_terminal():
    ledger = reserved(capacity=1)
    ledger.apply(event(EventKind.NOT_SENT, BackendConfirmation.NOT_ACCEPTED))
    with pytest.raises(ReservationRejected, match="retention full"):
        ledger.reserve(FEATURES, decision(ledger.snapshot()[0], "attempt-b"), CONTEXT)


def test_unknown_attempt_cannot_create_load():
    ledger = ReferenceLedger((worker(),))
    with pytest.raises(LifecycleRejected):
        ledger.apply(event(EventKind.SEND_STARTED))
    assert ledger.outstanding(REF) == 0
