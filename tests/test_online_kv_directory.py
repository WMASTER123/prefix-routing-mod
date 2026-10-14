"""Continuity/identity failures withdraw trust without inventing residency."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from prefix_routing_mod.terminal_journal import Binding
from tools.hust_native_probe import service_environment
from tools.online_kv_directory import (
    MAX_FRAME,
    NativeCursor,
    check_binding,
    receive_frames,
)


def batch(sequence, *, parent=None):
    return {
        "sequence": sequence,
        "ops": [{"kind": "store", "id": "x", "parent": parent, "local": "a" * 64}],
    }


def cursor(send=None, window=128):
    calls = []
    return NativeCursor({"worker": "a"}, send or calls.append, window=window), calls


def test_live_and_replay_duplicate_only_apply_once():
    c, calls = cursor()
    c.accept(batch(0))
    c.accept(batch(0))
    assert c.next == 1
    assert c.accepted == c.operations == c.duplicates == 1
    assert [v["kind"] for v in calls] == ["live"]


@pytest.mark.parametrize("value", [batch(2), batch(-1), batch(True), batch(2**64 - 1)])
def test_bad_sequence_fences_without_applying(value):
    c, calls = cursor()
    with pytest.raises(ValueError):
        c.accept(value)
    assert c.next == 0 and c.failed
    assert [v["kind"] for v in calls] == ["disconnect"]
    with pytest.raises(RuntimeError):
        c.accept(batch(0))


def test_conflicting_duplicate_removes_prior_trust():
    c, calls = cursor()
    c.accept(batch(0))
    with pytest.raises(ValueError, match="duplicate"):
        c.accept(batch(0, parent="different"))
    assert [v["kind"] for v in calls] == ["live", "disconnect"]


def test_unretained_duplicate_does_not_silently_reopen():
    c, calls = cursor(window=2)
    for seq in range(3):
        c.accept(batch(seq))
    assert len(c.recent) == 2
    with pytest.raises(ValueError, match="unretained"):
        c.accept(batch(0))
    assert calls[-1]["kind"] == "disconnect"


def test_index_bad_parent_or_budget_failure_cannot_advance_cursor():
    calls = []

    def send(value):
        calls.append(value["kind"])
        if value["kind"] == "live":
            raise ValueError("original directory rejected parent")

    c, _ = cursor(send)
    with pytest.raises(ValueError):
        c.accept(batch(0, parent="absent"))
    assert c.next == 0 and c.failed and not c.recent
    assert calls == ["live", "disconnect"]


def source_and_reply():
    source = SimpleNamespace(
        binding=Binding("a", "instance", "observer"),
        frontend_pid=10,
        core_pid=11,
    )
    reply = {
        "schema_version": 1,
        "binding": asdict(source.binding),
        "frontend_pid": 10,
        "core_pid": 11,
        "disabled": False,
        "mode": "replay",
    }
    return source, reply


def test_kv_identity_poll_can_follow_another_terminal_consumers_ack():
    source, reply = source_and_reply()
    reply["mode"] = "snapshot"
    check_binding(reply, source)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("frontend_pid", 12),
        ("core_pid", True),
        ("disabled", True),
        ("schema_version", True),
    ],
)
def test_changed_control_or_instance_is_not_a_cache_refresh(key, value):
    source, reply = source_and_reply()
    reply[key] = value
    with pytest.raises(ValueError):
        check_binding(reply, source)


def test_changed_instance_and_observer_fail():
    source, reply = source_and_reply()
    reply["binding"]["instance_epoch"] = "replacement"
    with pytest.raises(ValueError):
        check_binding(reply, source)


def test_two_replica_environment_has_explicit_worker_and_no_old_hook(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PREFIX_ROUTING_HUST_PUBLISHER", "1")
    monkeypatch.setenv("PREFIX_ROUTING_TERMINAL_TOKEN", "inherited-secret")
    monkeypatch.setenv("PYTHONPATH", "old-sdk")
    env = service_environment(
        1, tmp_path, tmp_path / "acl", token="a" * 64, worker="replica-b"
    )
    assert env["ASCEND_RT_VISIBLE_DEVICES"] == "1"
    assert env["PREFIX_ROUTING_TERMINAL_WORKER"] == "replica-b"
    assert env["PREFIX_ROUTING_TERMINAL_TOKEN"] == "a" * 64
    assert env["PYTHONPATH"] == str(tmp_path / "acl")
    assert "PREFIX_ROUTING_HUST_PUBLISHER" not in env
    assert "prefix_routing_terminal" in env["VLLM_PLUGINS"].split(",")


def test_multipart_count_is_bounded_before_receiving_attacker_tail():
    class Socket:
        calls = 0

        def recv(self):
            self.calls += 1
            return b"x"

        def getsockopt(self, _):
            return True

    sock = Socket()
    with pytest.raises(ValueError, match="frame count"):
        receive_frames(sock, SimpleNamespace(RCVMORE=1), 3)
    assert sock.calls == 3


def test_multipart_bytes_are_bounded_before_decode():
    class Socket:
        def recv(self):
            return b"x" * MAX_FRAME

        def getsockopt(self, _):
            return True

    with pytest.raises(ValueError, match="aggregate"):
        receive_frames(Socket(), SimpleNamespace(RCVMORE=1), 3)
