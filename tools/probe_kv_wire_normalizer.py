"""Encode synthetic events with the fixed HUST Struct definitions, normalize.

Imports only msgspec at explicit execution, never torch/vllm or device code.
The output trace feeds the existing native Rust index; no real KV is generated.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from prefix_routing_mod.hust_publisher import PUBLISHER_SHA256
from tools.kv_identity import local_block_digest
from tools.kv_wire_normalizer import batch_ops, live_batch


def load_codec(source, msgspec):
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != PUBLISHER_SHA256:
        raise ValueError("fixed HUST KV source fingerprint mismatch")
    tree = ast.parse(payload.decode())
    names = {
        "EventBatch",
        "KVCacheEvent",
        "BlockStored",
        "BlockRemoved",
        "AllBlocksCleared",
        "KVEventBatch",
    }
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in names
    ]
    if len(selected) != len(names):
        raise ValueError("native codec class set changed")
    environment = {
        "__name__": "fixed_hust_cpu_codec",
        "msgspec": msgspec,
        "ExternalBlockHash": bytes | int,
        "Any": Any,
    }
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"),
        environment,
    )
    return environment


def generate(source, msgspec):
    native = load_codec(source, msgspec)
    stored = native["BlockStored"](
        block_hashes=[11, 12],
        parent_block_hash=None,
        token_ids=[1, 2, 3, 4],
        block_size=2,
        lora_id=None,
        medium="GPU",
        lora_name=None,
        group_idx=0,
    )
    encode = msgspec.msgpack.encode
    decode = msgspec.msgpack.decode
    batch = native["KVEventBatch"]
    clear = native["AllBlocksCleared"]()
    epoch = "1" * 32
    initial = encode(batch(ts=0.0, events=[clear, stored], data_parallel_rank=0))
    removed = native["BlockRemoved"](block_hashes=[12], medium="GPU", group_idx=0)
    deltas = [
        encode(batch(ts=1.0, events=[removed], data_parallel_rank=0)),
        encode(batch(ts=2.0, events=[clear], data_parallel_rank=0)),
    ]
    normalized = [
        live_batch(
            [b"", sequence.to_bytes(8, "big"), payload, epoch.encode()],
            decode,
            publisher_epoch=epoch,
            block_size=2,
        )
        for sequence, payload in enumerate(deltas, 10)
    ]
    return {
        "binding": {
            "worker": "synthetic-worker",
            "instance": 1,
            "publisher": epoch,
            "identity": hashlib.sha256(b"synthetic-full-identity").hexdigest(),
            "block_size": 2,
        },
        "snapshot": batch_ops(initial, decode, 2),
        "watermark": 10,
        "events": normalized,
        "query": [local_block_digest([1, 2]).hex(), local_block_digest([3, 4]).hex()],
        "limit_bytes": 4 * 1024 * 1024,
        "lease_ns": 100,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; preserve prior evidence")
    import msgspec

    trace = generate(args.source_file, msgspec)
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / "trace.json").open("x", encoding="utf-8") as stream:
        json.dump(trace, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "status": "generated",
                "scope": "fixed native codec/synthetic events",
                "msgspec_version": msgspec.__version__,
                "native_index_executed": False,
                "npu_validated": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
