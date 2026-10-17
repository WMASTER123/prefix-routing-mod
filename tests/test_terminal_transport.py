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
    assert not source.records and not source.seen
    assert source.observe("b") and source.observe("c")
    assert not source.observe("d") and source.failed
    with pytest.raises(RuntimeError, match="unavailable"):
        source.poll(source.epoch, 1, 1)


def test_core_ack_releases_duplicate_guard_at_exact_record_boundary():
    source = CoreEnds(1)
    assert source.observe("child", "FINISHED_STOPPED")
    source.poll(source.epoch, 1, 1)

    # Reuse after ACK is a new observation. The fixed HUST adapter guarantees
    # one cleanup callback per concrete internal request; its randomized
    # internal identity may legally be reused only by a later request.
    assert source.observe("child", "FINISHED_ABORTED")
    replay = source.poll(source.epoch, 1, 1)
    assert replay["records"] == [
        {"sequence": 2, "child": "child", "status": "FINISHED_ABORTED"}
    ]


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
    assert state.cursor == 2 and not core.source.seen


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


def test_external_family_cannot_retire_in_observe_only_mode():
    core, state = frontend()
    state.journal.register("prm-external", ("child",))
    core.source.observe("child")
    reply = asyncio.run(state.read(None, 0, 0))
    epoch = reply["binding"]["observer_epoch"]
    asyncio.run(state.read(epoch, 1, 1))
    with pytest.raises(RuntimeError, match="exclusive Router"):
        asyncio.run(state.retire(epoch, 1, 1))
    assert "prm-external" in state.journal.families


def test_exclusive_router_family_retires_after_exact_durable_cursor():
    core, state = frontend()
    state.ownership_mode = "exclusive_router_v1"
    state.journal.begin_managed("prm-owned", 1)
    state.journal.bind_child("prm-owned", "opaque-child")
    state.journal.seal("prm-owned")
    core.source.observe("opaque-child", "FINISHED_STOPPED")
    reply = asyncio.run(state.read(None, 0, 0))
    epoch = reply["binding"]["observer_epoch"]
    asyncio.run(state.read(epoch, 1, 1))

    with pytest.raises(ValueError, match="cursor"):
        asyncio.run(state.retire(epoch, 1, 0))
    retired = asyncio.run(state.retire(epoch, 1, 1))
    assert retired["retired"] == 1
    assert retired["family_count"] == retired["reserved_children"] == 0
    assert not state.child_statuses
    repeated = asyncio.run(state.retire(epoch, 1, 1))
    assert repeated["retired"] == 0 and repeated["retired_through"] == 1

    # A consumer that lost a cursor older than the retired boundary must stop.
    with pytest.raises(RuntimeError, match="window exhausted"):
        asyncio.run(state.read(epoch, 0, 0))


def test_exclusive_router_reclaims_cumulatively_without_lifetime_cap():
    core, state = frontend()
    state.ownership_mode = "exclusive_router_v1"
    epoch = state.journal.binding.observer_epoch
    for number in range(600):
        attempt = f"prm-{number}"
        child = f"opaque-{number}"
        state.journal.begin_managed(attempt, 1)
        state.journal.bind_child(attempt, child)
        state.journal.seal(attempt)
        core.source.observe(child, "FINISHED_STOPPED")
        reply = asyncio.run(state.read(epoch, number, number))
        assert reply["boundary"] == number + 1
        asyncio.run(state.read(epoch, number + 1, number + 1))
        retired = asyncio.run(state.retire(epoch, number + 1, number + 1))
        assert retired["family_count"] == 0
        assert retired["reserved_children"] == 0
        assert not core.source.records and not core.source.seen

    assert state.journal.next_sequence == 601
    assert state.journal.retired_through == 600


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
