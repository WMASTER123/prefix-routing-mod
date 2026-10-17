"""In-process seam composition; no proxy, tokenizer, KV index or engine."""

from dataclasses import replace

import pytest

from prefix_routing_mod.contracts import (
    BackendConfirmation,
    CachePort,
    DispatchContext,
    EventKind,
    FeaturePort,
    Health,
    LedgerPort,
    PolicyPort,
    Precision,
    ReadContext,
    RequestFeatures,
    ReservationRejected,
    RouteDecision,
    Trust,
    WorkerRef,
)
from tests.fixtures.component_ledger import ReferenceLedger
from tests.test_component_contracts import CONTEXT, FEATURES, REF, event, match, worker


class StaticFeatures:
    def __init__(self, features):
        self.features = features

    def extract(self, request_id, request):
        assert request is None  # No user request data retained by this fixture.
        return replace(self.features, request_id=request_id)


class StaticDirectory:
    def __init__(self, matches):
        self.matches = matches  # Static observations, never an event index.

    def lookup(self, features, context):
        return self.matches


class FixturePolicy:
    """First eligible target only; exact matches label evidence, not a strategy."""

    def select(self, features, matches, workers, dispatch, context):
        if features.request_id != dispatch.request_id:
            raise ValueError("dispatch/request identity mismatch")
        target = next(
            (
                state
                for state in workers
                if state.eligible_for(features.domain, context)
            ),
            None,
        )
        if target is None:
            return None
        exact = any(
            value.cache.worker == target.ref and value.usable_for(features, context)
            for value in matches
        )
        return RouteDecision(
            "fixture-decision",
            features.request_id,
            dispatch.attempt_id,
            target.ref,
            target.version,
            "fixture-only",
            dispatch.config_version,
            "exact_evidence" if exact else "cache_unknown_fallback",
        )


def select_and_reserve(
    features: FeaturePort,
    cache: CachePort,
    state: LedgerPort,
    policy: PolicyPort,
    context: ReadContext = CONTEXT,
) -> RouteDecision | None:
    extracted = features.extract("request-a", None)
    selected = policy.select(
        extracted,
        cache.lookup(extracted, context),
        state.snapshot(),
        DispatchContext(extracted.request_id, "attempt-a", "config-1"),
        context,
    )
    if selected is not None:
        state.reserve(extracted, selected, context)
    return selected


@pytest.mark.parametrize(
    "features,observation,reason",
    [
        (FEATURES, match(), "exact_evidence"),
        (
            FEATURES,
            match(trust=Trust.RECOVERING, matched_tokens=None),
            "cache_unknown_fallback",
        ),
        (FEATURES, match(clock_id="backend-clock"), "cache_unknown_fallback"),
        (
            RequestFeatures(
                "request-a", "domain-a", None, None, None, None, Precision.UNKNOWN
            ),
            match(),
            "cache_unknown_fallback",
        ),
    ],
)
def test_base_chain_routes_with_explicit_precision(features, observation, reason):
    ledger = ReferenceLedger((worker(),))
    selected = select_and_reserve(
        StaticFeatures(features),
        StaticDirectory((observation,)),
        ledger,
        FixturePolicy(),
    )
    assert selected.reason_code == reason
    assert ledger.outstanding(REF) == 1
    # Simulated M5 lifecycle only: no assertion about actual HTTP cancellation.
    ledger.apply(event(EventKind.SEND_STARTED))
    ledger.apply(event(EventKind.STREAMING, sequence=2))
    ledger.apply(event(EventKind.COMPLETED, BackendConfirmation.ENDED, 3))
    assert ledger.outstanding(REF) == 0


def test_cache_evidence_from_old_instance_is_not_current_worker_evidence():
    next_ref = WorkerRef(REF.worker_id, "service-2")
    ledger = ReferenceLedger((worker(ref=next_ref),))
    selected = select_and_reserve(
        StaticFeatures(FEATURES), StaticDirectory((match(),)), ledger, FixturePolicy()
    )
    assert selected.worker == next_ref
    assert selected.reason_code == "cache_unknown_fallback"


def test_publisher_failure_leaves_backend_routable_and_ledger_intact():
    ledger = ReferenceLedger((worker(),))
    directory = StaticDirectory((match(),))
    select_and_reserve(StaticFeatures(FEATURES), directory, ledger, FixturePolicy())
    directory.matches = (match(trust=Trust.UNKNOWN, matched_tokens=None),)
    assert ledger.outstanding(REF) == 1
    assert ledger.snapshot()[0].eligible_for("domain-a", CONTEXT)


def test_no_eligible_worker_does_not_reserve_or_send():
    ledger = ReferenceLedger((worker(health=Health.UNHEALTHY),))
    assert (
        select_and_reserve(
            StaticFeatures(FEATURES),
            StaticDirectory((match(),)),
            ledger,
            FixturePolicy(),
        )
        is None
    )
    assert ledger.outstanding(REF) == 0


def test_commit_race_must_fail_before_m5_sends():
    ledger = ReferenceLedger((worker(),))

    class RacyPolicy(FixturePolicy):
        def select(self, features, matches, workers, dispatch, context):
            selected = super().select(features, matches, workers, dispatch, context)
            ledger.update_worker(replace(workers[0], version=1, draining=True))
            return selected

    with pytest.raises(ReservationRejected):
        select_and_reserve(
            StaticFeatures(FEATURES), StaticDirectory(()), ledger, RacyPolicy()
        )
    assert ledger.outstanding(REF) == 0
