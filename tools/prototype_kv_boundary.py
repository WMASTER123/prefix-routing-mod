"""CPU proof of a common snapshot/replay cut using fixed HUST mirror methods.

No registry installation, engine edit, sockets or NPU work. This evaluates one
candidate commit order; token-bearing recovery and epoch transport are separate.
"""

import argparse
import json
import queue
import threading
from collections import deque
from pathlib import Path

if __package__:
    from . import restore_runtime
else:
    import restore_runtime


class PublisherBoundaryPrototype:
    """One-owner-thread model, wrapping native mirror and replay methods."""

    def __init__(self, publisher, encode):
        self.publisher = publisher
        self.encode = encode
        self.owner = threading.get_ident()
        self.failed = False

    def enqueue(self, batch):
        # This models the candidate producer path: queue only, no mirror edit.
        self.publisher._event_queue.put_nowait(batch)

    def commit_next(self, send):
        self._check_owner()
        if self.failed:
            raise RuntimeError("Commit state is unavailable")
        event = self.publisher._event_queue.get_nowait()
        try:
            # Encoding must succeed before any mirror or watermark advances.
            payload = self.encode(event)
            seq = self.publisher._next_seq
            self.publisher._apply_to_mirror(event.events)
            self.publisher._buffer.append((seq, payload))
            self.publisher._next_seq = seq + 1
        except Exception:
            self.failed = True
            raise
        finally:
            self.publisher._event_queue.task_done()
        # A PUB send failure must leave the committed replay available.
        send(seq, payload)
        return seq

    def recover(self, request):
        self._check_owner()
        if self.failed:
            raise RuntimeError("Commit state is unavailable")
        return self.publisher._build_replay_response(request)

    def _check_owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError("Snapshot and commit need the same owner thread")


def inspect(module, msgspec):
    publisher = module.ZmqEventPublisher.__new__(module.ZmqEventPublisher)
    publisher._data_parallel_rank = 0
    publisher._event_queue = queue.Queue(maxsize=4)
    publisher._state_lock = threading.Lock()
    publisher._group_block_sizes = {}
    publisher._group_hashes = {}
    publisher._publisher_epoch = "synthetic-common-cut"
    publisher._next_seq = 0
    publisher._buffer = deque(maxlen=4)
    publisher._pack = msgspec.msgpack.Encoder()
    prototype = PublisherBoundaryPrototype(publisher, publisher._pack.encode)
    stored = module.KVEventBatch(
        ts=1,
        events=[
            module.BlockStored(
                block_hashes=[1],
                parent_block_hash=None,
                token_ids=[1, 2],
                block_size=2,
                lora_id=None,
                medium="GPU",
                lora_name=None,
                group_idx=0,
            )
        ],
    )
    removed = module.KVEventBatch(
        ts=2,
        events=[
            module.BlockRemoved(
                block_hashes=[1],
                medium="GPU",
                group_idx=0,
            )
        ],
    )
    request = module.ZmqEventReplayRequest(
        publisher_epoch=publisher._publisher_epoch, start_seq=0, force_snapshot=True
    )

    def snapshot_hashes():
        response = prototype.recover(request)
        batch = msgspec.msgpack.decode(response.snapshot, type=module.KVEventBatch)
        return response.next_seq, {
            h
            for event in batch.events
            if isinstance(event, module.BlockStored)
            for h in event.block_hashes
        }

    prototype.enqueue(stored)
    prototype.enqueue(removed)
    before = snapshot_hashes()
    prototype.commit_next(lambda *_: None)
    after_store = snapshot_hashes()
    prototype.commit_next(lambda *_: None)
    after_remove = snapshot_hashes()
    replay = prototype.recover(
        module.ZmqEventReplayRequest(
            publisher_epoch=publisher._publisher_epoch, start_seq=0
        )
    )
    consumer = set(before[1])
    for _, payload in replay.replayed_batches:
        batch = msgspec.msgpack.decode(payload, type=module.KVEventBatch)
        for event in batch.events:
            if isinstance(event, module.BlockStored):
                consumer.update(event.block_hashes)
            elif isinstance(event, module.BlockRemoved):
                consumer.difference_update(event.block_hashes)
    checks = {
        "pending_changes_do_not_enter_snapshot": before == (0, set()),
        "store_snapshot_cut": after_store == (1, {1}),
        "remove_snapshot_cut": after_remove == (2, set()),
        "snapshot_then_replay_converges": consumer == after_remove[1],
        "replay_sequences": [seq for seq, _ in replay.replayed_batches] == [0, 1],
    }
    return {
        "schema_version": 1,
        "scope": "synthetic_common_cut_prototype",
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "engine_hook_installed": False,
        "npu_events_validated": False,
        "complete_recovery_validated": False,
        "performance_validated": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--core-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.core_source.resolve(strict=True)
    identity = restore_runtime.read_manifest()["sources"]["core"]
    restore_runtime.verify_source(source, identity)
    output = restore_runtime.private_path(args.output)
    if output.exists():
        parser.error("Output already exists")
    import msgspec
    from vllm.distributed import kv_events as events

    if not Path(events.__file__).resolve().is_relative_to(source):
        parser.error("Imported publisher differs from audited source")
    report = inspect(events, msgspec)
    report["source_commit"] = identity["commit"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
