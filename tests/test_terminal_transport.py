import asyncio
from copy import deepcopy

import pytest

from prefix_routing_mod.terminal_transport import CORE_METHOD, CoreEnds, FrontendEnds


class Core:
    def __init__(self):
        self.source = CoreEnds()
        self.alter = lambda reply: reply
        self.installed = True

    async def call_utility_async(self, method, epoch, after, acknowledge):
        assert method == CORE_METHOD
        reply = self.source.poll(epoch, after, acknowledge)
        reply["cleanup_observer_installed"] = self.installed
        return self.alter(reply)


def frontend():
    core = Core()
    state = FrontendEnds(core, "replica", core.source.poll(None, 0, 0))
    state.managed_prefix = "prm-"
    return core, state


def test_core_ack_duplicate_and_finite_retention():
    source = CoreEnds(2)
    assert source.observe("a") and source.observe("a") and source.boundary == 1
    assert len(source.poll(source.epoch, 0, 0)["records"]) == 1
    assert source.poll(source.epoch, 1, 1)["records"] == []
    assert not source.records and set(source.seen) == {"a"}
    assert source.observe("b")
    assert not source.observe("c") and source.failed
    with pytest.raises(RuntimeError, match="unavailable"):
        source.poll(source.epoch, 1, 1)


@pytest.mark.parametrize(
    "epoch,after,ack",
    [("wrong", 0, 0), (None, 1, 0), (None, True, 0), (None, 0, -1), (None, 0, 1)],
)
def test_core_invalid_cursor_does_not_advance(epoch, after, ack):
    source = CoreEnds()
    with pytest.raises(ValueError):
        source.poll(epoch, after, ack)
    assert source.acknowledged == 0 and not source.failed


def test_core_replay_window_cannot_invent_missing_history():
    source = CoreEnds()
    source.observe("child")
    source.poll(source.epoch, 1, 1)
    with pytest.raises(ValueError):
        source.poll(source.epoch, 0, 0)


def test_frontend_replay_snapshot_and_foreign_traffic():
    core, state = frontend()
    state.journal.register("prm-a", ("managed",))
    core.source.observe("foreign")
    core.source.observe("managed")
    reply = asyncio.run(state.read(None, 0, 0))
    assert reply["boundary"] == 1 and reply["records"][0]["attempt_id"] == "prm-a"
    epoch = reply["binding"]["observer_epoch"]
    # Consumer lost the reply, so it re-reads before acknowledging.
    assert asyncio.run(state.read(epoch, 0, 0))["records"] == reply["records"]
    assert asyncio.run(state.read(epoch, 1, 1))["records"] == []
    recovered = asyncio.run(state.read(epoch, 0, 0))
    assert recovered["mode"] == "snapshot" and recovered["records"] == reply["records"]
    assert state.cursor == 2 and len(core.source.seen) == 2


def test_identity_is_independent_of_retired_frontend_history_and_ack():
    core, state = frontend()
    first = asyncio.run(state.identity())
    assert first["core_boundary"] == first["frontend_boundary"] == 0
    assert first["family_count"] == 0
    capture = state.journal.begin_owned(1)
    child = state.journal.allocate_child(capture)
    state.journal.bind_child(capture, child)
    state.journal.seal(capture)
    core.source.observe(child)
    observed = asyncio.run(state.identity())
    assert observed["core_boundary"] == observed["frontend_boundary"] == 1
    assert state.journal.acknowledged == 0
    state.journal.acknowledge(state.journal.binding, 1)
    state.journal.retire(state.journal.binding, 1, checkpointed_through=1)
    retired = asyncio.run(state.identity())
    assert retired["family_count"] == 0
    assert retired["binding"] == first["binding"]
    assert state.journal.acknowledged == 1
    assert "records" not in retired and "families" not in retired
    core.installed = False
    with pytest.raises(ValueError, match="identity/coverage"):
        asyncio.run(state.identity())


@pytest.mark.parametrize(
    "failure",
    [
        "epoch",
        "pid",
        "sequence",
        "coverage",
        "child",
        "boolean-schema",
        "observer-replaced",
    ],
)
def test_bad_core_batch_never_releases_or_advances(failure):
    core, state = frontend()
    state.journal.register("prm-a", ("managed",))
    core.source.observe("managed")

    def corrupt(reply):
        reply = deepcopy(reply)
        if failure == "epoch":
            reply["epoch"] = "foreign"
        elif failure == "pid":
            reply["pid"] += 1
        elif failure == "sequence":
            reply["records"][0]["sequence"] = 2
        elif failure == "coverage":
            reply["boundary"] += 1
        elif failure == "child":
            reply["records"][0]["child"] = 1
        elif failure == "boolean-schema":
            reply["schema_version"] = True
        else:
            reply["cleanup_observer_installed"] = False
        return reply

    core.alter = corrupt
    with pytest.raises(ValueError):
        asyncio.run(state.read(None, 0, 0))
    assert state.cursor == 0 and state.journal.failed
    assert not state.journal.families["prm-a"].ended


def test_cancelled_poll_invalidates_observation_without_ack():
    core, state = frontend()

    async def cancelled(*args):
        raise asyncio.CancelledError

    core.call_utility_async = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(state.read(None, 0, 0))
    assert state.journal.failed and state.cursor == 0 and core.source.acknowledged == 0


def test_wrong_frontend_epoch_does_not_touch_core():
    core, state = frontend()
    with pytest.raises(ValueError):
        asyncio.run(state.read("wrong", 0, 0))
    assert not state.journal.failed and core.source.acknowledged == 0


def test_corrupt_batch_tail_cannot_apply_valid_head():
    core, state = frontend()
    state.journal.register("prm-a", ("first", "second"))
    core.source.observe("first")
    core.source.observe("second")

    def corrupt(reply):
        reply["records"][1]["sequence"] = 99
        return reply

    core.alter = corrupt
    with pytest.raises(ValueError):
        asyncio.run(state.read(None, 0, 0))
    assert not state.journal.families["prm-a"].ended and state.cursor == 0


def test_same_child_cannot_change_terminal_reason():
    source = CoreEnds()
    assert source.observe("child", "FINISHED_ABORTED")
    assert not source.observe("child", "FINISHED_STOPPED")
    assert source.failed
