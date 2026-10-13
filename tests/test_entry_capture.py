"""Whole HTTP attempts, not one currently visible sampling group."""

import pytest

from tools.entry_capture_prototype import EntryCapture
from tools.terminal_observer_prototype import Binding, TerminalJournal


def capture(sizes=(2, 2)):
    journal = TerminalJournal(
        Binding("worker", "instance", "observer"),
        max_families=1,
        max_children=8,
        max_events=1,
    )
    return EntryCapture(journal, "attempt", sizes)


def test_early_group_end_cannot_finish_http_attempt():
    entry = capture()
    for group in ("opaque-A", "opaque-B"):
        entry.register_group(group, 2)
        for child in (f"{group}:random-x", f"{group}:random-y"):
            entry.bind(group, child)
            assert entry.journal.observe_core_end(child)
            assert entry.journal.snapshot(entry.journal.binding) == (0, ())
        entry.submitted(group)
        if group == "opaque-A":
            assert entry.journal.snapshot(entry.journal.binding) == (0, ())
    cut, records = entry.journal.snapshot(entry.journal.binding)
    assert cut == 1 and records[0].attempt_id == "attempt"


@pytest.mark.parametrize("sizes", [(), [], (0,), (True,), (9,), (1,) * 9])
def test_invalid_plan_rejected_before_submission(sizes):
    with pytest.raises((ValueError, RuntimeError)):
        capture(sizes)


def test_known_children_ended_after_partial_failure_stay_unresolved():
    entry = capture()
    entry.register_group("first", 2)
    entry.bind("first", "a")
    entry.abandon()
    assert entry.journal.observe_core_end("a")
    assert entry.journal.snapshot(entry.journal.binding) == (0, ())
    with pytest.raises(ValueError):
        entry.bind("first", "b")


def test_group_ids_and_cardinality_checked():
    entry = capture()
    with pytest.raises(ValueError):
        entry.register_group("g", 3)
    entry.register_group("g", 2)
    with pytest.raises(ValueError):
        entry.register_group("g", 2)
    entry.bind("g", "child")
    with pytest.raises(ValueError):
        entry.bind("g", "child")
    with pytest.raises(ValueError):
        entry.submitted("g")


def test_unconsumed_second_generator_never_seals():
    entry = capture((1, 1))
    entry.register_group("a", 1)
    entry.register_group("b", 1)
    entry.bind("a", "child")
    entry.journal.observe_core_end("child")
    entry.submitted("a")
    assert not entry.journal.families["attempt"].sealed
    assert entry.journal.snapshot(entry.journal.binding) == (0, ())
