"""Dynamic receiver has no precharges and never duplicates send permission."""

from dataclasses import asdict

import pytest

from prefix_routing_mod.contracts import BackendConfirmation, EventKind, LifecycleEvent
from tools.durable_ledger_prototype import DurableLedger
from tools.online_transport_probe import CLOCK, REF, AdmissionReceiver, fixture_worker
from tools.transport_receiver_probe import SCHEMA

TOKEN = "ab" * 32


def request(index=1):
    return {
        "schema": "admission-cpu-v1",
        "token": TOKEN,
        "request_id": f"request-{index}",
        "attempt_id": f"attempt-{index}",
        "clock_id": CLOCK,
        "at_ns": 10,
    }


def test_dynamic_request_then_transport_to_same_ledger(tmp_path):
    with DurableLedger(
        tmp_path / "log", create=True, workers=(fixture_worker(),)
    ) as store:
        receiver = AdmissionReceiver(store, TOKEN)
        assert store.outstanding(REF) == 0
        assert receiver.handle(request())["result"] == "applied"
        assert store.outstanding(REF) == 1
        record = LifecycleEvent(
            "event-1",
            "request-1",
            "attempt-1",
            REF,
            1,
            EventKind.SEND_STARTED,
            BackendConfirmation.NONE,
            CLOCK,
            11,
        )
        assert (
            receiver.handle(
                {"schema": SCHEMA, "token": TOKEN, "event": asdict(record)}
            )["result"]
            == "applied"
        )
        assert store.oracle._attempts["attempt-1"].state == "sending"


def test_lost_admission_ack_restart_cannot_grant_again(tmp_path):
    path = tmp_path / "log"
    with DurableLedger(path, create=True, workers=(fixture_worker(),)) as store:
        # Discard a successfully committed reply, exactly like a lost ACK.
        AdmissionReceiver(store, TOKEN).handle(request())
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == 1 and not store.admission_ready
        assert AdmissionReceiver(store, TOKEN).handle(request())["result"] == "rejected"


def test_duplicate_live_permission_and_quota_never_resend(tmp_path):
    with DurableLedger(
        tmp_path / "log", create=True, workers=(fixture_worker(),), per_worker_limit=1
    ) as store:
        receiver = AdmissionReceiver(store, TOKEN)
        assert receiver.handle(request())["result"] == "applied"
        assert receiver.handle(request(2))["result"] == "rejected"
        assert store.outstanding(REF) == 1
        assert not store.failed and store.admission_ready
        # A rejected new request must not disconnect observations for old work.
        record = LifecycleEvent(
            "sent-1",
            "request-1",
            "attempt-1",
            REF,
            1,
            EventKind.SEND_STARTED,
            BackendConfirmation.NONE,
            CLOCK,
            11,
        )
        assert (
            receiver.handle(
                {"schema": SCHEMA, "token": TOKEN, "event": asdict(record)}
            )["result"]
            == "applied"
        )
    with DurableLedger(
        tmp_path / "second", create=True, workers=(fixture_worker(),)
    ) as store:
        receiver = AdmissionReceiver(store, TOKEN)
        receiver.handle(request())
        assert receiver.handle(request())["result"] == "rejected"
        assert store.operation_count == 1


@pytest.mark.parametrize(
    "change", [{"clock_id": "old"}, {"token": "00" * 32}, {"extra": 1}, {"at_ns": -1}]
)
def test_bad_admission_envelope_never_creates_work(tmp_path, change):
    with DurableLedger(
        tmp_path / "log", create=True, workers=(fixture_worker(),)
    ) as store:
        with pytest.raises(ValueError):
            AdmissionReceiver(store, TOKEN).handle(request() | change)
        assert store.outstanding(REF) == 0


def test_entry_source_guard_precedes_execution(tmp_path):
    from tools.audit_backend_terminal import load_sources
    from tools.audit_http_entry import ENTRY_HASHES

    (tmp_path / "base_serving.py").write_text("raise RuntimeError('never execute')")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_sources(tmp_path, ENTRY_HASHES)
