"""Deterministic, CPU-only audit of the fixed HUST publisher's recovery boundary.

Synthetic protocol events are explicit; this is not real NPU eviction/recovery.
The installed publisher methods are exercised without changing engine sources.
"""

import argparse
import json
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path

CORE_COMMIT = "13f11da44455d2f85f00f810a04e1bd2eaf30ba6"


def inspect_boundary(module, msgspec) -> dict:
    publisher = module.ZmqEventPublisher.__new__(module.ZmqEventPublisher)
    # A paused publisher thread is represented by its real queue plus a fixed
    # watermark. No sockets, device work or background loops are started.
    publisher._running = True
    publisher._data_parallel_rank = 0
    publisher._replay_endpoint = "ipc://synthetic-unused"
    publisher._event_queue = queue.Queue(maxsize=4)
    publisher._state_lock = threading.Lock()
    publisher._group_block_sizes = {}
    publisher._group_hashes = {}
    publisher._publisher_epoch = "synthetic-epoch"
    publisher._next_seq = 1
    publisher._pack = msgspec.msgpack.Encoder()
    cleared = module.KVEventBatch(ts=0, events=[module.AllBlocksCleared()])
    publisher._buffer = deque([(0, publisher._pack.encode(cleared))], maxlen=4)
    stored = module.BlockStored(
        block_hashes=[1],
        parent_block_hash=None,
        token_ids=list(range(128)),
        block_size=128,
        lora_id=None,
        medium="GPU",
        lora_name=None,
        group_idx=0,
    )
    publisher.publish(module.KVEventBatch(ts=1, events=[stored]))
    request = module.ZmqEventReplayRequest(
        publisher_epoch="synthetic-epoch", start_seq=1, force_snapshot=True
    )
    response = publisher._build_replay_response(request)
    snapshot = msgspec.msgpack.decode(response.snapshot, type=module.KVEventBatch)
    snapshots = [e for e in snapshot.events if isinstance(e, module.BlockStored)]
    report = {
        "source_methods_exercised": ["publish", "_build_replay_response"],
        "queued_unpublished_batches": publisher._event_queue.qsize(),
        "publisher_next_seq": publisher._next_seq,
        "snapshot_next_seq": response.next_seq,
        "snapshot_hash_count": sum(len(e.block_hashes) for e in snapshots),
        "snapshot_token_count": sum(len(e.token_ids) for e in snapshots),
        "snapshot_parent_count": sum(
            e.parent_block_hash is not None for e in snapshots
        ),
        "mirror_update_precedes_sequence_assignment": (
            publisher._event_queue.qsize() == 1
            and response.next_seq == 1
            and sum(len(e.block_hashes) for e in snapshots) == 1
        ),
    }
    removed = module.BlockRemoved(block_hashes=[1], medium="GPU", group_idx=0)
    publisher.publish(module.KVEventBatch(ts=2, events=[removed]))
    after_remove = publisher._build_replay_response(request)
    final_snapshot = msgspec.msgpack.decode(
        after_remove.snapshot, type=module.KVEventBatch
    )
    report["queued_after_remove"] = publisher._event_queue.qsize()
    report["snapshot_hashes_after_unpublished_remove"] = sum(
        len(event.block_hashes)
        for event in final_snapshot.events
        if isinstance(event, module.BlockStored)
    )
    report["watermark_after_unpublished_remove"] = after_remove.next_seq
    # Use actual queued HUST batches to demonstrate the temporal regression.
    # This synthetic set is only a counterexample, not a production KV index.
    consumer_hashes = set()
    first_pending = publisher._event_queue.get_nowait()
    for event in first_pending.events:
        if isinstance(event, module.BlockStored):
            consumer_hashes.update(event.block_hashes)
    report["naive_snapshot_then_old_store_reintroduces_evicted_hash"] = (
        1 in consumer_hashes and report["snapshot_hashes_after_unpublished_remove"] == 0
    )
    second_pending = publisher._event_queue.get_nowait()
    for event in second_pending.events:
        if isinstance(event, module.BlockRemoved):
            consumer_hashes.difference_update(event.block_hashes)
    report["naive_consumer_converges_only_after_pending_remove"] = not consumer_hashes
    report["status"] = (
        "observed"
        if report["mirror_update_precedes_sequence_assignment"]
        else "unexpected"
    )
    report["complete_recovery_validated"] = False
    report["npu_events_validated"] = False
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--core-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.core_source.resolve(strict=True)
    if args.output.exists():
        parser.error("Output already exists")
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    if commit != CORE_COMMIT:
        parser.error("This audit targets only the recorded HUST core commit")
    changes = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    if changes:
        parser.error("Publisher source has local changes")
    import msgspec
    import vllm.distributed.kv_events as events

    if not Path(events.__file__).resolve().is_relative_to(source):
        parser.error("Imported publisher is not from the supplied source")
    report = inspect_boundary(events, msgspec)
    report.update(
        {
            "schema_version": 1,
            "scope": "synthetic_publisher_boundary_audit",
            "source_commit": commit,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps(report))
    return 0 if report["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
