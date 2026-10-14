"""Live in-process callbacks into the existing ledger, not post-run replay."""

from dataclasses import replace

import pytest

from prefix_routing_mod.contracts import BackendConfirmation, EventKind
from tests.test_component_contracts import REF, event, reserved
from tools.terminal_observer_prototype import (
    Binding,
    Ended,
    OnlineConsumer,
    TerminalJournal,
)

BINDING = Binding(REF.worker_id, REF.instance_epoch, "observer-1")


def journal(**changes):
    limits = dict(max_families=4, max_children=8, max_events=4)
    return TerminalJournal(BINDING, **(limits | changes))


def live_pair(children=("child",)):
    source = journal()
    source.register("attempt-a", children)
    ledger = reserved()
    ledger.apply(event(EventKind.SEND_STARTED))
    ledger.apply(event(EventKind.OUTCOME_UNKNOWN, sequence=2))
    delivered = []

    def apply(record):
        assert record.binding == BINDING
        # This fixture has explicitly cancelled all children; the generic
        # journal itself never infers success/cancel reason from logical end.
        terminal = event(
            EventKind.CANCEL_CONFIRMED,
            BackendConfirmation.ENDED,
            sequence=3,
            event_id=f"terminal-{record.sequence}",
            attempt_id=record.attempt_id,
        )
        result = ledger.apply(terminal)
        if result not in ("applied", "duplicate"):
            raise RuntimeError("ledger did not accept terminal")
        delivered.append(result)

    consumer = OnlineConsumer(BINDING, apply)
    consumer.recover(source)
    return source, ledger, consumer, delivered


def test_online_delivery_only_after_all_cancelled_children():
    source, ledger, consumer, delivered = live_pair(("a", "b"))
    assert source.observe_core_end("a")
    consumer.pump(source)
    assert ledger.outstanding(REF) == 1
    assert not delivered
    assert source.observe_core_end("b")
    consumer.pump(source)
    assert ledger.outstanding(REF) == 0
    assert delivered == ["applied"]
    assert source.observe_core_end("b")
    consumer.pump(source)
    assert delivered == ["applied"]


def test_lost_ack_recovery_does_not_double_release(monkeypatch):
    source, ledger, consumer, delivered = live_pair()
    source.observe_core_end("child")
    acknowledge = source.acknowledge

    def fail(*args):
        raise ConnectionError("synthetic acknowledgement loss")

    monkeypatch.setattr(source, "acknowledge", fail)
    with pytest.raises(ConnectionError):
        consumer.pump(source)
    assert consumer.requires_recovery
    assert consumer.cursor == 0
    assert ledger.outstanding(REF) == 0
    monkeypatch.setattr(source, "acknowledge", acknowledge)
    consumer.recover(source)
    assert delivered == ["applied", "duplicate"]


def test_disconnect_and_consumer_restart_with_retained_ledger():
    source, ledger, consumer, delivered = live_pair()
    consumer.disconnect()
    source.observe_core_end("child")
    with pytest.raises(RuntimeError, match="recovery"):
        consumer.pump(source)
    assert ledger.outstanding(REF) == 1
    restarted = OnlineConsumer(BINDING, consumer.apply_terminal)
    restarted.recover(source)
    assert ledger.outstanding(REF) == 0
    again = OnlineConsumer(BINDING, consumer.apply_terminal)
    again.recover(source)
    assert delivered == ["applied", "duplicate"]


def test_observer_restart_does_not_clear_old_instance_debt():
    source, ledger, consumer, _ = live_pair()
    replacement = journal()
    replacement.binding = replace(BINDING, observer_epoch="observer-2")
    with pytest.raises(ValueError, match="binding"):
        consumer.recover(replacement)
    assert consumer.requires_recovery
    assert ledger.outstanding(REF) == 1
    assert not source.families["attempt-a"].ended


def test_empty_snapshot_does_not_prove_not_accepted():
    source, ledger, consumer, _ = live_pair()
    consumer.recover(source)
    assert ledger.outstanding(REF) == 1


def test_sink_failure_no_ack_and_recovery(monkeypatch):
    source, ledger, consumer, delivered = live_pair()
    source.observe_core_end("child")
    apply = consumer.apply_terminal

    def fail(record):
        raise ConnectionError("synthetic delivery loss")

    monkeypatch.setattr(consumer, "apply_terminal", fail)
    with pytest.raises(ConnectionError):
        consumer.pump(source)
    assert source.acknowledged == 0 and ledger.outstanding(REF) == 1
    monkeypatch.setattr(consumer, "apply_terminal", apply)
    consumer.recover(source)
    assert delivered == ["applied"] and ledger.outstanding(REF) == 0


@pytest.mark.parametrize("malformation", ["gap", "foreign", "truncated"])
def test_malformed_snapshot_rejected_before_apply(monkeypatch, malformation):
    source, ledger, consumer, delivered = live_pair()
    source.observe_core_end("child")
    _, records = source.snapshot(BINDING)
    if malformation == "gap":
        records = (replace(records[0], sequence=2),)
    elif malformation == "foreign":
        records = (replace(records[0], binding=replace(BINDING, instance_epoch="old")),)
    else:
        records = ()
    monkeypatch.setattr(source, "snapshot", lambda binding: (1, records))
    with pytest.raises(ValueError):
        consumer.recover(source)
    assert not delivered and ledger.outstanding(REF) == 1
    assert consumer.requires_recovery


def test_event_overflow_poisons_observation_not_scheduler():
    source = journal(max_events=1)
    source.register("one", ("a",))
    source.register("two", ("b",))
    assert source.observe_core_end("a")
    assert source.observe_core_end("b") is False
    assert source.failed and len(source.events) == 1
    with pytest.raises(RuntimeError, match="unavailable"):
        source.snapshot(BINDING)
    with pytest.raises(RuntimeError, match="unavailable"):
        source.register("three", ("c",))


def test_retention_full_rejects_new_admission_without_mutation():
    source = journal(max_families=1)
    source.register("a", ("one",))
    source.observe_core_end("one")
    source.acknowledge(BINDING, 1)
    with pytest.raises(RuntimeError, match="retention"):
        source.register("b", ("two",))
    assert list(source.families) == ["a"]
    assert len(source.owners) == 1


def test_unknown_child_fails_closed():
    source = journal()
    assert not source.observe_core_end("unregistered")
    assert source.failed


@pytest.mark.parametrize("children", [(), ["a"], ("a", "a"), ("a\n",), ("x" * 97,)])
def test_invalid_registration(children):
    source = journal()
    with pytest.raises(ValueError):
        source.register("attempt", children)
    assert not source.families and not source.owners


def test_child_and_family_identity_cannot_be_reused():
    source = journal()
    source.register("a", ("child",))
    with pytest.raises(ValueError):
        source.register("b", ("child",))
    with pytest.raises(ValueError):
        source.register("a", ("other",))


def test_replay_window_requires_snapshot():
    source = journal()
    source.register("a", ("child",))
    source.observe_core_end("child")
    source.acknowledge(BINDING, 1)
    with pytest.raises(RuntimeError, match="window"):
        source.replay(BINDING, 0)
    boundary, records = source.snapshot(BINDING)
    assert boundary == 1 and len(records) == 1


@pytest.mark.parametrize("cursor", [-1, True, 2, 1.5])
def test_invalid_cursors(cursor):
    source = journal()
    with pytest.raises(ValueError):
        source.acknowledge(BINDING, cursor)


def test_ended_record_validation():
    with pytest.raises(ValueError):
        Ended(BINDING, True, "a")
    with pytest.raises(ValueError):
        Binding("a", "b", "")


def test_dynamic_child_completion_waits_for_seal():
    source = journal()
    source.begin("dynamic", 2)
    source.bind_child("dynamic", "first")
    assert source.observe_core_end("first")
    assert source.snapshot(BINDING) == (0, ())
    with pytest.raises(ValueError, match="incomplete"):
        source.seal("dynamic")
    source.bind_child("dynamic", "second")
    assert source.observe_core_end("second")
    assert source.snapshot(BINDING) == (0, ())
    assert source.seal("dynamic")
    assert source.snapshot(BINDING)[0] == 1
    assert source.seal("dynamic")  # Idempotent sealing cannot emit twice.
    assert source.snapshot(BINDING)[0] == 1


def test_partial_submission_failure_never_confirms_family():
    source = journal()
    source.begin("partial", 3)
    source.bind_child("partial", "first")
    source.abandon("partial")
    assert source.observe_core_end("first")
    assert source.snapshot(BINDING) == (0, ())
    with pytest.raises(RuntimeError, match="abandoned"):
        source.seal("partial")


def test_child_capacity_reserved_before_submission():
    source = journal(max_children=3)
    source.begin("first", 3)
    with pytest.raises(RuntimeError, match="retention"):
        source.begin("second", 1)
    assert source.reserved_children == 3 and not source.owners


def test_dynamic_capture_rejects_late_child_and_count_overflow():
    source = journal()
    source.begin("one", 1)
    source.bind_child("one", "child")
    with pytest.raises(ValueError, match="exceeded"):
        source.bind_child("one", "extra")
    source.seal("one")
    with pytest.raises(RuntimeError, match="closed"):
        source.bind_child("one", "late")


def test_seal_overflow_invalidates_observation():
    source = journal(max_events=1)
    source.register("one", ("a",))
    source.observe_core_end("a")
    source.begin("two", 1)
    source.bind_child("two", "b")
    source.observe_core_end("b")
    assert source.seal("two") is False
    assert source.failed and len(source.events) == 1


def test_managed_family_reclaims_capacity_only_after_durable_cursor():
    source = journal(max_families=1, max_children=1, max_events=1)
    attempt = source.begin_owned(1)
    child = source.allocate_child(attempt)
    source.bind_child(attempt, child)
    source.seal(attempt)
    assert source.observe_core_end(child)
    source.acknowledge(BINDING, 1)

    with pytest.raises(ValueError, match="durable consumer cursor"):
        source.retire(BINDING, 1, checkpointed_through=0)
    assert source.retire(BINDING, 1, checkpointed_through=1) == 1
    assert source.reserved_children == 0 and not source.families and not source.owners

    next_attempt = source.begin_owned(1)
    assert next_attempt != attempt
    assert source.allocate_child(next_attempt) != child


def test_retirement_requires_owner_allocated_nonreused_identity():
    source = journal(max_families=1, max_children=1, max_events=1)
    source.register("external", ("child",))
    source.observe_core_end("child")
    source.acknowledge(BINDING, 1)
    with pytest.raises(RuntimeError, match="external attempt identity"):
        source.retire(BINDING, 1, checkpointed_through=1)
    assert "external" in source.families and source.reserved_children == 1


def test_recovery_after_retirement_requires_persisted_cursor():
    source = journal(max_families=1, max_children=1, max_events=1)
    attempt = source.begin_owned(1)
    child = source.allocate_child(attempt)
    source.bind_child(attempt, child)
    source.seal(attempt)
    source.observe_core_end(child)
    source.acknowledge(BINDING, 1)
    source.retire(BINDING, 1, checkpointed_through=1)

    fresh = OnlineConsumer(BINDING, lambda _: None)
    with pytest.raises(RuntimeError, match="durable cursor"):
        fresh.recover(source)
    resumed = OnlineConsumer(BINDING, lambda _: None)
    resumed.cursor = 1  # Loaded from the consumer's committed checkpoint.
    resumed.recover(source)
    assert not resumed.requires_recovery and resumed.cursor == 1


def test_managed_retirement_runs_beyond_original_family_and_child_limits():
    source = journal(max_families=1, max_children=1, max_events=1)
    identities = set()
    for boundary in range(1, 301):
        attempt = source.begin_owned(1)
        child = source.allocate_child(attempt)
        assert attempt not in identities and child not in identities
        identities.update((attempt, child))
        source.bind_child(attempt, child)
        source.seal(attempt)
        assert source.observe_core_end(child)
        source.acknowledge(BINDING, boundary)
        assert source.retire(BINDING, boundary, checkpointed_through=boundary) == 1
    assert source.next_sequence == 301
    assert source.retired_through == 300
    assert source.reserved_children == 0 and not source.families and not source.events
