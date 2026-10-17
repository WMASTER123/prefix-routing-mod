"""Explicit CPU handover retains old debt and rejects old send authority."""

import subprocess
import sys
from dataclasses import replace

import pytest

from prefix_routing_mod.contracts import (
    EventKind,
    Health,
    ReadContext,
    ReservationRejected,
)
from tests.test_component_contracts import (
    CONTEXT,
    FEATURES,
    REF,
    decision,
    event,
    worker,
)
from tools.durable_ledger_prototype import DurableLedger
from tools.terminal_observer_prototype import Binding, Ended

NEW = ReadContext("runtime-new", 5, 5)


def observation(store, context=NEW):
    old = store.oracle.snapshot()[0]
    return replace(
        old,
        version=old.version + 1,
        clock_id=context.clock_id,
        at_ns=context.now_ns,
        backend_running=replace(
            old.backend_running, clock_id=context.clock_id, at_ns=context.now_ns
        ),
        local_outstanding=replace(
            old.local_outstanding, clock_id=context.clock_id, at_ns=context.now_ns
        ),
    )


def initialize(path, *, sent=True, **kwargs):
    with DurableLedger(path, create=True, workers=(worker(),), **kwargs) as store:
        store.reserve(FEATURES, decision(), CONTEXT)
        if sent:
            store.receive_transport(event(EventKind.SEND_STARTED))


def resume(store, context=NEW):
    return store.resume(
        (observation(store, context),), context, previous_dispatcher_stopped=True
    )


def test_new_runtime_admits_dynamically_but_retains_old_work(tmp_path):
    path = tmp_path / "log"
    initialize(path)
    with DurableLedger(path) as store:
        assert resume(store) == "applied" and store.outstanding(REF) == 1
        state = store.oracle.snapshot()[0]
        store.reserve(
            replace(FEATURES, request_id="new-request"),
            decision(state, "new-attempt", "new-request"),
            NEW,
        )
        assert store.outstanding(REF) == 2
        store.receive_transport(
            event(
                EventKind.OUTCOME_UNKNOWN,
                sequence=2,
                error_class="transport_eof",
                at_ns=200,
            )
        )
        assert store.oracle.snapshot()[0].local_outstanding.at_ns == 5
        assert (
            store.confirm_end(
                Ended(
                    Binding(REF.worker_id, REF.instance_epoch, "observer"),
                    1,
                    "attempt-a",
                ),
                EventKind.COMPLETED,
                NEW,
            )
            == "applied"
        )
        assert store.outstanding(REF) == 1
    with DurableLedger(path) as store:
        assert not store.admission_ready and store.outstanding(REF) == 1
        assert store.oracle._attempts["attempt-a"].clock_id == CONTEXT.clock_id
        assert store.oracle._attempts["new-attempt"].clock_id == NEW.clock_id


@pytest.mark.parametrize("method", ["apply", "notify", "batch"])
def test_old_reserved_permission_cannot_send_after_handover(tmp_path, method):
    path = tmp_path / "log"
    initialize(path, sent=False)
    with DurableLedger(path) as store:
        resume(store)
        with pytest.raises(ReservationRejected, match="old"):
            if method == "apply":
                store.apply(event(EventKind.SEND_STARTED))
            elif method == "notify":
                store.notify_transport("attempt-a", EventKind.SEND_STARTED, CONTEXT)
            else:
                from dataclasses import asdict

                store.receive_batch(
                    ({"kind": "event", "event": asdict(event(EventKind.SEND_STARTED))},)
                )
        assert store.outstanding(REF) == 1
        # Historical observation is allowed, but provides no send permission.
        assert store.receive_transport(event(EventKind.SEND_STARTED)) == "applied"


@pytest.mark.parametrize(
    "change", ["stale", "clock", "identity", "count", "health", "version"]
)
def test_invalid_takeover_remains_paused(tmp_path, change):
    path = tmp_path / "log"
    initialize(path)
    with DurableLedger(path) as store:
        state = observation(store)
        context = replace(NEW, now_ns=100) if change == "stale" else NEW
        if change == "clock":
            state = worker(version=1)
        if change == "identity":
            state = replace(state, ref=replace(REF, instance_epoch="other"))
        if change == "count":
            state = replace(
                state, local_outstanding=replace(state.local_outstanding, value=0)
            )
        if change == "health":
            state = replace(state, health=Health.UNKNOWN)
        if change == "version":
            state = replace(state, version=0)
        with pytest.raises(ReservationRejected):
            store.resume((state,), context, previous_dispatcher_stopped=True)
        assert not store.admission_ready and store.outstanding(REF) == 1


def test_old_dispatcher_stop_and_never_reused_clock_required(tmp_path):
    path = tmp_path / "log"
    initialize(path)
    with DurableLedger(path) as store:
        with pytest.raises(ReservationRejected, match="stop"):
            store.resume((observation(store),), NEW)
        resume(store)
    with DurableLedger(path) as store:
        with pytest.raises(ReservationRejected, match="reused"):
            resume(store)
        assert not store.admission_ready


@pytest.mark.parametrize(
    "point,committed", [("before_commit", False), ("after_commit_before_ack", True)]
)
def test_takeover_commit_failure_does_not_open_dispatch(tmp_path, point, committed):
    path = tmp_path / "log"
    initialize(path)

    def fail(where):
        if where == point:
            raise OSError("injected fault")

    with DurableLedger(path, fault=fail) as store:
        with pytest.raises(OSError):
            resume(store)
        assert not store.admission_ready
    with DurableLedger(path) as store:
        assert not store.admission_ready
        assert (NEW.clock_id in store.oracle._runtime_clocks) == committed
        assert store.outstanding(REF) == 1


def test_unknown_old_work_still_enforces_new_runtime_quota(tmp_path):
    path = tmp_path / "log"
    initialize(path, per_worker_limit=1)
    with DurableLedger(path) as store:
        resume(store)
        with pytest.raises(ReservationRejected, match="limit"):
            store.reserve(
                replace(FEATURES, request_id="new"),
                decision(store.oracle.snapshot()[0], "new", "new"),
                NEW,
            )
        assert store.outstanding(REF) == 1


@pytest.mark.parametrize(
    "point,committed",
    [
        ("before_commit", False),
        ("after_commit_before_ack", True),
    ],
)
def test_process_exit_during_runtime_takeover(tmp_path, point, committed):
    path = tmp_path / "log"
    initialize(path)
    code = (
        "import os,sys; from pathlib import Path; "
        "from tools.durable_ledger_prototype import DurableLedger; "
        "from tests.test_runtime_recovery import resume; "
        "store=DurableLedger(Path(sys.argv[1]), "
        "fault=lambda point: os._exit(37) if point==sys.argv[2] else None); "
        "resume(store)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), point],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 37, result.stderr
    with DurableLedger(path) as store:
        assert not store.admission_ready and store.outstanding(REF) == 1
        assert (NEW.clock_id in store.oracle._runtime_clocks) == committed


def test_failed_handover_can_retry_fresh_observation_without_reopen(tmp_path):
    path = tmp_path / "log"
    initialize(path)
    with DurableLedger(path) as store:
        with pytest.raises(ReservationRejected):
            store.resume(
                (observation(store),),
                replace(NEW, now_ns=100),
                previous_dispatcher_stopped=True,
            )
        assert not store.failed and not store.admission_ready
        assert resume(store) == "applied"
