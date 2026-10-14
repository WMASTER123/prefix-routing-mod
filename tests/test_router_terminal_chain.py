"""Combined boundaries, using the existing owner and explicit synthetic source."""

from copy import deepcopy
from dataclasses import replace

import pytest

from prefix_routing_mod.contracts import EventKind, ReadContext, WorkerRef
from tests.test_dynamic_admission import TOKEN, request
from tools.durable_ledger_prototype import DurableLedger
from tools.online_transport_probe import AdmissionReceiver, fixture_worker
from tools.router_terminal_chain import TerminalSource


def fixture(tmp_path):
    worker = replace(
        fixture_worker(), ref=WorkerRef("actual-worker", "actual-instance")
    )
    store = DurableLedger(
        tmp_path / "ledger", create=True, workers=(worker,), per_worker_limit=1
    )
    receiver = AdmissionReceiver(store, TOKEN, worker_ref=worker.ref)
    binding = {
        "worker_id": worker.ref.worker_id,
        "instance_epoch": worker.ref.instance_epoch,
        "observer_epoch": "observer",
    }
    reply = {
        "schema_version": 1,
        "mode": "replay",
        "binding": binding,
        "core_pid": 100,
        "frontend_pid": 200,
        "boundary": 0,
        "records": [],
        "families": [],
        "disabled": False,
    }

    def read(*args):
        return deepcopy(reply)

    source = TerminalSource(read)
    return store, receiver, reply, source, worker


def end(reply, attempt="attempt-1"):
    reply.update(
        boundary=1,
        records=[{"binding": reply["binding"], "sequence": 1, "attempt_id": attempt}],
        families=[
            {
                "attempt_id": attempt,
                "expected": 1,
                "bound": 1,
                "ended": 1,
                "sealed": True,
                "abandoned": False,
                "core_statuses": {"FINISHED_ABORTED": 1},
            }
        ],
    )


def test_real_binding_not_fixture_and_http_cannot_clear_debt(tmp_path):
    store, receiver, reply, source, worker = fixture(tmp_path)
    with store:
        assert receiver.handle(request())["result"] == "applied"
        store.notify_transport(
            "attempt-1", EventKind.SEND_STARTED, ReadContext(worker.clock_id, 11, 300)
        )
        end(reply)
        source.consume(store, ReadContext(worker.clock_id, 12, 300))
        assert store.outstanding(worker.ref) == 1  # No business outcome yet.
        assert receiver.handle(request(2))["result"] == "rejected"
        source.outcomes["attempt-1"] = EventKind.CANCEL_CONFIRMED
        source.consume(store, ReadContext(worker.clock_id, 13, 300))
        assert store.outstanding(worker.ref) == 0
        assert receiver.handle(request(3))["result"] == "applied"
        assert len(store.terminal_receipts) == 1


@pytest.mark.parametrize("fault", ["epoch", "core", "gap", "tail", "disabled"])
def test_bad_source_preserves_debt_and_fences_new_admission(tmp_path, fault):
    store, _, reply, source, worker = fixture(tmp_path)
    with store:
        receiver = AdmissionReceiver(
            store, TOKEN, worker_ref=worker.ref, before_reserve=source.check
        )
        receiver.handle(request())
        store.notify_transport(
            "attempt-1", EventKind.SEND_STARTED, ReadContext(worker.clock_id, 11, 300)
        )
        end(reply)
        source.outcomes["attempt-1"] = EventKind.COMPLETED
        if fault == "epoch":
            reply["binding"]["observer_epoch"] = "foreign"
        elif fault == "core":
            reply["core_pid"] = 101
        elif fault == "gap":
            reply["boundary"] = 2
        elif fault == "disabled":
            reply["disabled"] = True
        else:
            reply["records"].append(
                {"binding": reply["binding"], "sequence": 99, "attempt_id": "bad"}
            )
            reply["boundary"] = 2
        with pytest.raises(ValueError):
            source.consume(store, ReadContext(worker.clock_id, 12, 300))
        with pytest.raises(RuntimeError):
            receiver.handle(request(2))
        assert store.outstanding(worker.ref) == 1 and source.cursor == 0


def test_lost_permit_and_reopen_keep_actual_worker_debt(tmp_path):
    store, _, _, _, worker = fixture(tmp_path)

    def lost():
        raise ConnectionError("after commit")

    with store:
        receiver = AdmissionReceiver(
            store, TOKEN, worker_ref=worker.ref, after_admission=lost
        )
        with pytest.raises(ConnectionError):
            receiver.handle(request())
        assert store.outstanding(worker.ref) == 1 and not store.transport_receipts
    with DurableLedger(tmp_path / "ledger") as recovered:
        assert recovered.outstanding(worker.ref) == 1 and not recovered.admission_ready


def test_terminal_before_transport_waits_for_real_send(tmp_path):
    store, receiver, reply, source, worker = fixture(tmp_path)
    with store:
        receiver.handle(request())
        end(reply)
        source.outcomes["attempt-1"] = EventKind.COMPLETED
        source.consume(store, ReadContext(worker.clock_id, 12, 300))
        assert source.cursor == 0 and store.outstanding(worker.ref) == 1


def test_health_failure_cannot_create_a_reservation(tmp_path):
    store, _, _, _, worker = fixture(tmp_path)

    def unhealthy():
        raise TimeoutError("no health")

    with store:
        receiver = AdmissionReceiver(
            store, TOKEN, worker_ref=worker.ref, before_reserve=unhealthy
        )
        with pytest.raises(TimeoutError):
            receiver.handle(request())
        assert store.outstanding(worker.ref) == 0


def test_bad_terminal_family_tail_cannot_apply_head(tmp_path):
    store, receiver, reply, source, worker = fixture(tmp_path)
    with store:
        receiver.handle(request())
        store.notify_transport(
            "attempt-1", EventKind.SEND_STARTED, ReadContext(worker.clock_id, 11, 300)
        )
        end(reply)
        source.outcomes["attempt-1"] = EventKind.COMPLETED
        reply["records"].append(
            {"binding": reply["binding"], "sequence": 2, "attempt_id": "bad"}
        )
        reply["families"].append({"attempt_id": "bad", "sealed": False})
        reply["boundary"] = 2
        with pytest.raises(ValueError):
            source.consume(store, ReadContext(worker.clock_id, 12, 300))
        assert (
            source.failed and source.cursor == 0 and store.outstanding(worker.ref) == 1
        )
