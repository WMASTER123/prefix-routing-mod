import builtins
import hashlib
import pickle
import queue
import sys
from collections import deque
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from prefix_routing_mod import hust_publisher as hook


@dataclass
class Stored:
    block_hashes: list
    parent_block_hash: object = None
    token_ids: object = None
    block_size: int = 2
    lora_id: object = None
    medium: str = "GPU"
    lora_name: object = None
    extra_keys: object = None
    group_idx: int = 0


@dataclass
class Removed:
    block_hashes: list
    medium: str = "GPU"
    group_idx: int = 0


@dataclass
class Cleared:
    pass


@dataclass
class Batch:
    events: list
    ts: int = 0
    data_parallel_rank: int = 0


class NativePublisher:
    def _apply_to_mirror(self, events):
        for event in events:
            if isinstance(event, Stored):
                self._group_block_sizes[0] = event.block_size
                self._group_hashes.setdefault(0, set()).update(event.block_hashes)
            elif isinstance(event, Removed):
                self._group_hashes.setdefault(0, set()).difference_update(
                    event.block_hashes
                )
            elif isinstance(event, Cleared):
                self._group_hashes.clear()

    def _build_replay_response(self, request):
        if request.force_snapshot:
            return SimpleNamespace(
                snapshot=pickle.dumps(self._build_snapshot_batch()), replayed_batches=[]
            )
        return SimpleNamespace(snapshot=None, replayed_batches=list(self._buffer))


def make(limits=None):
    limits = limits or hook.Limits()
    native = SimpleNamespace(
        ZmqEventPublisher=NativePublisher,
        BlockStored=Stored,
        BlockRemoved=Removed,
        AllBlocksCleared=Cleared,
        KVEventBatch=Batch,
        logger=Mock(),
        EventPublisherFactory=SimpleNamespace(_registry={"zmq": NativePublisher}),
    )
    codec = SimpleNamespace(
        structs=SimpleNamespace(replace=replace),
        msgpack=SimpleNamespace(Encoder=lambda: SimpleNamespace(encode=pickle.dumps)),
    )
    publisher = hook.make_publisher_type(native, codec, limits).__new__(
        hook.make_publisher_type(native, codec, limits)
    )
    publisher._metadata = {}
    publisher._metadata_tokens = 0
    publisher._replay_bytes = 0
    publisher._failure_reason = None
    publisher._group_hashes = {}
    publisher._group_block_sizes = {}
    publisher._buffer = deque(maxlen=4)
    publisher._next_seq = 0
    publisher._publisher_epoch = "initial"
    publisher._data_parallel_rank = 0
    publisher._pack = codec.msgpack.Encoder()
    publisher._event_queue = queue.Queue(maxsize=4)
    publisher._running = True
    publisher._replay = None
    publisher._topic_bytes = b""
    publisher._pub = Mock()
    return publisher, native, codec


def snapshot(publisher):
    response = publisher._build_replay_response(SimpleNamespace(force_snapshot=True))
    assert response["valid"]
    return response, pickle.loads(response["snapshot"])


def test_default_plugin_does_not_import_engine_or_mutate_binding(monkeypatch):
    monkeypatch.delenv("PREFIX_ROUTING_HUST_PUBLISHER", raising=False)
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert not name.startswith(("vllm", "torch", "msgspec", "zmq"))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    hook.load_plugin()


def test_guard_rejects_wrong_version_and_source(tmp_path, monkeypatch):
    source = tmp_path / "publisher.py"
    source.write_text("fixed source", encoding="utf-8")
    module = SimpleNamespace(__file__=str(source))
    versions = {"vllm": hook.CORE_VERSION, "vllm-ascend-hust": hook.ASCEND_VERSION}
    with pytest.raises(RuntimeError, match="fingerprint"):
        hook.verify_guard(module, versions.__getitem__)
    monkeypatch.setattr(
        hook, "PUBLISHER_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()
    )
    hook.verify_guard(module, versions.__getitem__)
    versions["vllm"] = "another"
    with pytest.raises(RuntimeError, match="versions"):
        hook.verify_guard(module, versions.__getitem__)


def test_explicit_allowlist_and_idempotent_factory_binding(monkeypatch):
    _, native, codec = make()
    monkeypatch.setitem(sys.modules, "msgspec", codec)
    monkeypatch.setitem(sys.modules, "vllm", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "vllm.distributed", SimpleNamespace(kv_events=native)
    )
    monkeypatch.setattr(hook, "verify_guard", lambda _: None)
    monkeypatch.setenv("PREFIX_ROUTING_HUST_PUBLISHER", "1")
    monkeypatch.setenv("VLLM_PLUGINS", "ascend")
    with pytest.raises(RuntimeError, match="allowlist"):
        hook.load_plugin()
    monkeypatch.setenv("VLLM_PLUGINS", "ascend," + hook.PLUGIN_NAME)
    hook.load_plugin()
    adapted = native.EventPublisherFactory._registry["zmq"]
    hook.load_plugin()
    assert native.EventPublisherFactory._registry["zmq"] is adapted
    monkeypatch.setenv("PREFIX_ROUTING_PUBLISHER_MAX_BLOCKS", "3")
    with pytest.raises(RuntimeError, match="binding"):
        hook.load_plugin()


def test_queued_store_and_remove_do_not_change_snapshot_until_commit():
    publisher, _, _ = make()
    stored = Batch([Stored([1], token_ids=[10, 11])])
    removed = Batch([Removed([1])])
    publisher.publish(stored)
    publisher.publish(removed)
    response, batch = snapshot(publisher)
    assert response["next_seq"] == 0 and batch.events == [Cleared()]
    publisher._commit(publisher._event_queue.get_nowait())
    response, batch = snapshot(publisher)
    assert response["next_seq"] == 1 and batch.events[1].token_ids == [10, 11]
    publisher._commit(publisher._event_queue.get_nowait())
    response, batch = snapshot(publisher)
    assert response["next_seq"] == 2 and batch.events == [Cleared()]


def test_snapshot_retains_evicted_parent_topology_without_residency():
    publisher, _, _ = make()
    publisher._commit(Batch([Stored([1, 2], token_ids=[10, 11, 12, 13])]))
    publisher._commit(Batch([Removed([1])]))
    _, batch = snapshot(publisher)
    assert [e.block_hashes for e in batch.events[1:]] == [[1], [2], [1]]
    assert batch.events[2].parent_block_hash == 1
    assert isinstance(batch.events[-1], Removed)
    assert publisher._group_hashes == {0: {2}}


def test_bounds_fail_closed_and_real_clear_creates_new_epoch():
    publisher, _, _ = make(hook.Limits(max_blocks=1))
    publisher._commit(Batch([Stored([1, 2], token_ids=[10, 11, 12, 13])]))
    response = publisher._build_replay_response(SimpleNamespace(force_snapshot=True))
    assert not response["valid"] and not publisher._metadata
    publisher._commit(Batch([Cleared()]))
    response, _ = snapshot(publisher)
    assert response["publisher_epoch"] != "initial" and response["next_seq"] == 1


def test_unneeded_history_pruned_before_metadata_limit_failure():
    publisher, _, _ = make(hook.Limits(max_blocks=1))
    publisher._commit(Batch([Stored([1], token_ids=[10, 11])]))
    publisher._commit(Batch([Removed([1])]))
    publisher._commit(Batch([Stored([2], token_ids=[20, 21])]))
    assert publisher._failure_reason is None and set(publisher._metadata) == {(0, 2)}


@pytest.mark.parametrize(
    "event",
    [
        Stored([1], token_ids=[]),
        Stored([1], token_ids=[1, 2], extra_keys=[("salt",)]),
        Stored([1], token_ids=[1, 2], group_idx=1),
    ],
)
def test_unsupported_metadata_does_not_produce_trusted_snapshot(event):
    publisher, _, _ = make()
    publisher._commit(Batch([event]))
    assert not publisher._build_replay_response(SimpleNamespace(force_snapshot=True))[
        "valid"
    ]


def test_pub_failure_keeps_replay_and_includes_epoch_in_actual_frame():
    publisher, _, _ = make()
    publisher._event_queue.put(Batch([Stored([1], token_ids=[1, 2])]))
    publisher._event_queue.put(None)
    publisher._running = False
    publisher._pub.send_multipart.side_effect = OSError("send failed")
    publisher._publisher_thread()
    assert publisher._next_seq == 1 and len(publisher._buffer) == 1
    assert publisher._failure_reason is None
    frames = publisher._pub.send_multipart.call_args.args[0]
    assert len(frames) == 4 and frames[-1] == b"initial"
    assert publisher._event_queue.unfinished_tasks == 0


def test_missing_ancestor_and_reply_limit_fail_closed():
    publisher, _, _ = make()
    publisher._commit(Batch([Stored([2], parent_block_hash=1, token_ids=[1, 2])]))
    assert not publisher._build_replay_response(SimpleNamespace(force_snapshot=True))[
        "valid"
    ]


def test_replay_retention_has_a_byte_bound_and_metadata_recovery_remains_available():
    publisher, _, _ = make(hook.Limits(max_reply_bytes=1024))
    publisher._buffer = deque(maxlen=100)
    for _ in range(20):
        publisher._commit(Batch([Cleared()]))
    assert publisher._replay_bytes == sum(
        len(payload) for _, payload in publisher._buffer
    )
    assert publisher._replay_bytes <= 1024 and len(publisher._buffer) < 20
    snapshot(publisher)
    publisher, _, _ = make(hook.Limits(max_reply_bytes=200))
    publisher._commit(Batch([Cleared()]))
    assert not publisher._build_replay_response(SimpleNamespace(force_snapshot=True))[
        "valid"
    ]
