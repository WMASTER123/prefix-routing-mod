"""Real SQLite receipts and child-process crash recovery of the CPU oracle."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from prefix_routing_mod.contracts import (
    BackendConfirmation,
    EventKind,
    LifecycleRejected,
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
from tools.durable_ledger_prototype import DurableLedger, decode
from tools.terminal_observer_prototype import Binding, OnlineConsumer, TerminalJournal


def opened(path, **kwargs):
    return DurableLedger(path, create=True, workers=(worker(),), **kwargs)


def charge(store):
    store.reserve(FEATURES, decision(), CONTEXT)
    store.apply(event(EventKind.SEND_STARTED))
    store.apply(event(EventKind.OUTCOME_UNKNOWN, sequence=2))


def terminal():
    return event(EventKind.CANCEL_CONFIRMED, BackendConfirmation.ENDED, sequence=3)


def test_restart_retains_debt_and_pauses_admission(tmp_path):
    path = tmp_path / "receipts.sqlite"
    with opened(path) as store:
        charge(store)
        assert store.outstanding(REF) == 1
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == 1
        assert not store.admission_ready
        with pytest.raises(ReservationRejected, match="fresh"):
            store.reserve(FEATURES, decision(), CONTEXT)
        assert store.apply(terminal()) == "applied"
        assert store.outstanding(REF) == 0
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == 0
        assert store.apply(terminal()) == "duplicate"
        assert store.operation_count == 4


def test_live_journal_ack_requires_durable_application(tmp_path, monkeypatch):
    binding = Binding(REF.worker_id, REF.instance_epoch, "observer-1")
    journal = TerminalJournal(binding, max_families=1, max_children=1, max_events=1)
    journal.register("attempt-a", ("child",))
    path = tmp_path / "receipts.sqlite"
    with opened(path) as store:
        charge(store)
        consumer = OnlineConsumer(
            binding,
            lambda record: store.confirm_end(
                record, EventKind.CANCEL_CONFIRMED, replace(CONTEXT, now_ns=110)
            ),
        )
        consumer.recover(journal)
        journal.observe_core_end("child")
        acknowledge = journal.acknowledge

        def lose_ack(*args):
            raise ConnectionError("synthetic ACK loss")

        monkeypatch.setattr(journal, "acknowledge", lose_ack)
        with pytest.raises(ConnectionError):
            consumer.pump(journal)
        assert journal.acknowledged == 0
        assert store.outstanding(REF) == 0
    # Both ledger and consumer are reconstructed, with a still-live observer.
    with DurableLedger(path) as store:
        monkeypatch.setattr(journal, "acknowledge", acknowledge)
        consumer = OnlineConsumer(
            binding,
            lambda record: store.confirm_end(
                record, EventKind.CANCEL_CONFIRMED, replace(CONTEXT, now_ns=120)
            ),
        )
        consumer.recover(journal)
        assert journal.acknowledged == 1 and store.operation_count == 4
        assert store.outstanding(REF) == 0


@pytest.mark.parametrize(
    "point,expected",
    [
        ("before_commit", 1),
        ("after_commit_before_ack", 0),
    ],
)
def test_forced_process_exit_at_receipt_boundary(tmp_path, point, expected):
    path = tmp_path / "receipts.sqlite"
    with opened(path) as store:
        charge(store)
    code = (
        "import os,sys; from pathlib import Path; "
        "from tools.durable_ledger_prototype import DurableLedger; "
        "from tests.test_durable_ledger_prototype import terminal; "
        "target=sys.argv[2]; "
        "fault=lambda where: os._exit(37) if where==target else None; "
        "store=DurableLedger(Path(sys.argv[1]),fault=fault); "
        "store.apply(terminal()); raise RuntimeError('fault did not execute')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), point],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 37, result.stderr
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == expected
        outcome = store.apply(terminal())
        assert outcome == ("applied" if expected else "duplicate")
        assert store.outstanding(REF) == 0 and store.operation_count == 4


@pytest.mark.parametrize("point", ["before_commit", "after_commit_before_ack"])
def test_transaction_error_never_acknowledges_or_authorizes_send(tmp_path, point):
    path = tmp_path / "receipts.sqlite"
    sends = []
    with opened(path) as store:
        store.reserve(FEATURES, decision(), CONTEXT)

        def fail(where):
            if where == point:
                raise OSError("synthetic storage failure")

        store.fault = fail
        with pytest.raises(OSError):
            store.apply(event(EventKind.SEND_STARTED))
            sends.append("network send")
        assert not sends and store.failed and not store.admission_ready
        assert store.outstanding(REF) == 1
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == 1
        assert store.operation_count == (1 if point == "before_commit" else 2)


def test_event_gap_poison_owner_without_mutating_committed_state(tmp_path):
    path = tmp_path / "receipts.sqlite"
    with opened(path) as store:
        store.reserve(FEATURES, decision(), CONTEXT)
        with pytest.raises(LifecycleRejected):
            store.apply(event(EventKind.OUTCOME_UNKNOWN, sequence=3))
        assert store.failed and store.operation_count == 1
    with DurableLedger(path) as store:
        assert store.outstanding(REF) == 1


def test_duplicate_and_conflicting_id(tmp_path):
    path = tmp_path / "receipts.sqlite"
    with opened(path) as store:
        charge(store)
        assert store.apply(event(EventKind.SEND_STARTED)) == "duplicate"
        assert store.operation_count == 3
        with pytest.raises(LifecycleRejected):
            store.apply(replace(event(EventKind.SEND_STARTED), at_ns=999))
    with DurableLedger(path) as store:
        assert store.operation_count == 3 and store.outstanding(REF) == 1


def test_operation_limit_rejects_without_clearing_debt(tmp_path):
    path = tmp_path / "receipts.sqlite"
    with opened(path, max_operations=3) as store:
        charge(store)
        with pytest.raises(RuntimeError, match="retention"):
            store.apply(terminal())
        assert store.outstanding(REF) == 1
    with DurableLedger(path) as store:
        assert store.operation_count == 3 and store.outstanding(REF) == 1


def test_payload_limit_rejects_before_receipt(tmp_path):
    with opened(tmp_path / "receipts.sqlite", max_payload_bytes=1) as store:
        with pytest.raises(RuntimeError, match="retention"):
            store.reserve(FEATURES, decision(), CONTEXT)
        assert store.operation_count == 0 and store.outstanding(REF) == 0


def test_missing_log_cannot_bootstrap_empty(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        DurableLedger(path)
    assert not path.exists()


def test_create_never_overwrites_existing_log(tmp_path):
    path = tmp_path / "existing"
    path.write_bytes(b"preserve me")
    with pytest.raises(FileExistsError):
        opened(path)
    assert path.read_bytes() == b"preserve me"


def test_corrupt_log_rejected(tmp_path):
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        DurableLedger(path)


def test_receipt_gap_rejected(tmp_path):
    path = tmp_path / "gap.sqlite"
    with opened(path) as store:
        charge(store)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM receipts WHERE seq=2")
    with pytest.raises(ValueError, match="sequence gap"):
        DurableLedger(path)


def test_receipts_exclude_prompt_and_tokens(tmp_path):
    path = tmp_path / "receipts.sqlite"
    with opened(path) as store:
        charge(store)
        text = "\n".join(
            row[0] for row in store.connection.execute("SELECT payload FROM receipts")
        )
        assert "token_ids" not in text and "identity" not in text
        assert "prompt" not in text
        assert json.loads(text.splitlines()[0])["domain"] == FEATURES.domain


def test_bootstrap_nonzero_load_not_silently_imported(tmp_path):
    state = worker()
    state = replace(state, local_outstanding=replace(state.local_outstanding, value=1))
    with pytest.raises(ValueError, match="invent"):
        DurableLedger(tmp_path / "bad.sqlite", create=True, workers=(state,))


def test_decode_rejects_duplicate_fields():
    with pytest.raises(ValueError, match="duplicate"):
        decode('{"kind":"event","kind":"reserve"}')


def test_module_import_has_no_database_or_device_work(tmp_path):
    code = (
        "import sys; import tools.durable_ledger_prototype; "
        "assert 'torch' not in sys.modules and 'vllm' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        env=os.environ.copy(),
        cwd=Path(__file__).resolve().parents[1],
        timeout=20,
    )
    assert result.returncode == 0 and not list(tmp_path.iterdir())


def test_single_owner_sequences_transport_and_end_with_late_callback(tmp_path):
    from tools.terminal_observer_prototype import Ended

    record = Ended(
        Binding(REF.worker_id, REF.instance_epoch, "observer-1"), 1, "attempt-a"
    )
    path = tmp_path / "sequencer.sqlite"
    with opened(path) as store:
        store.reserve(FEATURES, decision(), CONTEXT)
        assert (
            store.notify_transport("attempt-a", EventKind.SEND_STARTED, CONTEXT)
            == "applied"
        )
        assert (
            store.notify_transport("attempt-a", EventKind.SEND_STARTED, CONTEXT)
            == "no_transition"
        )
        assert (
            store.notify_transport("attempt-a", EventKind.OUTCOME_UNKNOWN, CONTEXT)
            == "applied"
        )
        assert (
            store.notify_transport("attempt-a", EventKind.OUTCOME_UNKNOWN, CONTEXT)
            == "no_transition"
        )
        assert (
            store.confirm_end(record, EventKind.CANCEL_CONFIRMED, CONTEXT) == "applied"
        )
        assert (
            store.notify_transport("attempt-a", EventKind.OUTCOME_UNKNOWN, CONTEXT)
            == "no_transition"
        )
        assert store.operation_count == 4 and store.outstanding(REF) == 0
    with DurableLedger(path) as store:
        assert (
            store.confirm_end(record, EventKind.CANCEL_CONFIRMED, CONTEXT)
            == "duplicate"
        )
        assert store.operation_count == 4
        with pytest.raises(LifecycleRejected, match="different evidence"):
            store.confirm_end(record, EventKind.COMPLETED, CONTEXT)


def test_transport_cannot_restart_send_from_retained_reservation(tmp_path):
    path = tmp_path / "reserved.sqlite"
    with opened(path) as store:
        store.reserve(FEATURES, decision(), CONTEXT)
    with DurableLedger(path) as store:
        with pytest.raises(ReservationRejected, match="restart"):
            store.notify_transport("attempt-a", EventKind.SEND_STARTED, CONTEXT)
        assert store.outstanding(REF) == 1


def test_foreign_observer_binding_cannot_release(tmp_path):
    from tools.terminal_observer_prototype import Ended

    with opened(tmp_path / "foreign.sqlite") as store:
        charge(store)
        wrong = Ended(
            Binding(REF.worker_id, "foreign-instance", "observer-1"), 1, "attempt-a"
        )
        with pytest.raises(ValueError, match="identity mismatch"):
            store.confirm_end(wrong, EventKind.CANCEL_CONFIRMED, CONTEXT)
        assert store.outstanding(REF) == 1 and store.operation_count == 3
