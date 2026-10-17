"""Experimental, explicitly enabled adapter for one fixed HUST publisher.

Import and default plugin loading are inert. Device execution is never touched.
This internal factory hook is not Manager activation of the routing MOD.
"""

import hashlib
import importlib.metadata
import os
import queue
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

PUBLISHER_SHA256 = "9eca5310ada1d90c4740c1cec06fbdb18aff65fc9d2abb536098fcbd0ddf7d14"
CORE_VERSION = "0.23.1.post1.dev733+g13f11da44.empty"
ASCEND_VERSION = "0.19.1.post1.dev563+g3a61053d"
PLUGIN_NAME = "prefix_routing_publisher"


@dataclass(frozen=True)
class Limits:
    max_blocks: int = 16384
    max_tokens: int = 2097152
    max_reply_bytes: int = 16 * 1024**2

    def __post_init__(self):
        for value, upper in (
            (self.max_blocks, 65536),
            (self.max_tokens, 8 * 1024**2),
            (self.max_reply_bytes, 64 * 1024**2),
        ):
            if type(value) is not int or not 0 < value <= upper:
                raise ValueError("Invalid publisher resource limit")


def verify_guard(module, version=importlib.metadata.version):
    if version("vllm") != CORE_VERSION or version("vllm-ascend-hust") != ASCEND_VERSION:
        raise RuntimeError("Experimental publisher requires the fixed HUST versions")
    if (
        hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        != PUBLISHER_SHA256
    ):
        raise RuntimeError("Experimental publisher source fingerprint differs")


def load_plugin():
    enabled = os.environ.get("PREFIX_ROUTING_HUST_PUBLISHER", "0")
    if enabled == "0":
        return
    if enabled != "1":
        raise ValueError("PREFIX_ROUTING_HUST_PUBLISHER must be 0 or 1")
    allowlist = os.environ.get("VLLM_PLUGINS", "").split(",")
    if PLUGIN_NAME not in allowlist:
        raise RuntimeError("Experimental publisher needs an explicit plugin allowlist")
    import msgspec
    from vllm.distributed import kv_events as native

    verify_guard(native)
    limits = Limits(
        max_blocks=int(os.environ.get("PREFIX_ROUTING_PUBLISHER_MAX_BLOCKS", "16384")),
        max_tokens=int(
            os.environ.get("PREFIX_ROUTING_PUBLISHER_MAX_TOKENS", "2097152")
        ),
        max_reply_bytes=int(
            os.environ.get(
                "PREFIX_ROUTING_PUBLISHER_MAX_REPLY_BYTES", str(16 * 1024**2)
            )
        ),
    )
    current = native.EventPublisherFactory._registry["zmq"]
    if getattr(current, "_prefix_routing_limits", None) == limits:
        return
    if current is not native.ZmqEventPublisher:
        raise RuntimeError("Publisher binding already changed or limits differ")
    adapted = make_publisher_type(native, msgspec, limits)
    native.EventPublisherFactory._registry["zmq"] = adapted


def make_publisher_type(native, codec, limits):
    """Build a subclass lazily; all recovery state belongs to its owner thread."""

    class PrefixRoutingPublisher(native.ZmqEventPublisher):
        _prefix_routing_limits = limits

        def __init__(self, data_parallel_rank, **kwargs):
            if data_parallel_rank != 0:
                raise ValueError("Experimental publisher supports DP rank 0 only")
            if kwargs.get("buffer_steps", 10000) <= 0:
                raise ValueError("Experimental publisher needs a replay buffer")
            self._metadata = {}
            self._metadata_tokens = 0
            self._replay_bytes = 0
            self._failure_reason = None
            super().__init__(data_parallel_rank=data_parallel_rank, **kwargs)

        def publish(self, events):
            if not self._running:
                raise RuntimeError("Publisher is closed")
            if events.data_parallel_rank is None:
                events.data_parallel_rank = self._data_parallel_rank
            # Retain native bounded queue/backpressure; mirror changes now
            # occur only on the publisher thread, with its replay watermark.
            self._event_queue.put(events)

        def _unavailable(self, reason):
            if self._failure_reason is None:
                self._publisher_epoch = uuid.uuid4().hex
                self._next_seq = 0
                self._buffer.clear()
                self._replay_bytes = 0
            self._failure_reason = reason
            self._metadata.clear()
            self._metadata_tokens = 0
            self._group_hashes.clear()
            self._group_block_sizes.clear()

        def _record_metadata(self, events):
            for event in events:
                if isinstance(event, native.AllBlocksCleared):
                    self._metadata.clear()
                    self._metadata_tokens = 0
                elif isinstance(event, native.BlockRemoved):
                    if event.group_idx != 0 or event.medium != "GPU":
                        raise ValueError("Unsupported removed-block identity")
                elif isinstance(event, native.BlockStored):
                    if (
                        event.group_idx != 0
                        or event.medium != "GPU"
                        or event.lora_id is not None
                        or event.lora_name is not None
                        or not event.block_hashes
                        or event.block_size <= 0
                        or len(event.token_ids)
                        != len(event.block_hashes) * event.block_size
                        or (
                            event.extra_keys is not None
                            and (
                                len(event.extra_keys) != len(event.block_hashes)
                                or any(key is not None for key in event.extra_keys)
                            )
                        )
                    ):
                        raise ValueError(
                            "Unsupported block identity or partial metadata"
                        )
                    parent = event.parent_block_hash
                    for i, block_hash in enumerate(event.block_hashes):
                        key = (0, block_hash)
                        tokens = list(
                            event.token_ids[
                                i * event.block_size : (i + 1) * event.block_size
                            ]
                        )
                        block = codec.structs.replace(
                            event,
                            block_hashes=[block_hash],
                            token_ids=tokens,
                            parent_block_hash=parent,
                            extra_keys=None if event.extra_keys is None else [None],
                        )
                        previous = self._metadata.get(key)
                        if previous is not None and (
                            previous.token_ids != tokens
                            or previous.parent_block_hash != parent
                            or previous.block_size != event.block_size
                        ):
                            raise ValueError("Conflicting block metadata")
                        if previous is None:
                            self._metadata_tokens += len(tokens)
                        self._metadata[key] = block
                        parent = block_hash

        def _ordered_metadata(self):
            ordered, seen = [], set()
            for group, hashes in self._group_hashes.items():
                for block_hash in hashes:
                    key, path, active = (group, block_hash), [], set()
                    while key not in seen:
                        if key in active:
                            raise ValueError("Cyclic parent metadata")
                        active.add(key)
                        block = self._metadata.get(key)
                        if block is None:
                            raise ValueError("Missing parent metadata")
                        path.append((key, block))
                        if block.parent_block_hash is None:
                            break
                        key = (group, block.parent_block_hash)
                    for key, block in reversed(path):
                        if key not in seen:
                            seen.add(key)
                            ordered.append((key, block))
            return ordered

        def _prune_metadata(self):
            self._metadata = dict(self._ordered_metadata())
            self._metadata_tokens = sum(
                len(b.token_ids) for b in self._metadata.values()
            )

        def _commit(self, event):
            payload = self._pack.encode(event)
            if len(payload) > limits.max_reply_bytes:
                self._unavailable("event_payload_limit")
                raise ValueError("Event exceeds publisher payload limit")
            cleared = any(isinstance(e, native.AllBlocksCleared) for e in event.events)
            if self._failure_reason is not None and cleared:
                self._failure_reason = None
                self._buffer.clear()
                self._replay_bytes = 0
                self._publisher_epoch = uuid.uuid4().hex
                self._next_seq = 0
            if self._failure_reason is None:
                try:
                    self._record_metadata(event.events)
                    super()._apply_to_mirror(event.events)
                    if (
                        len(self._metadata) > limits.max_blocks
                        or self._metadata_tokens > limits.max_tokens
                    ):
                        self._prune_metadata()
                    if (
                        len(self._metadata) > limits.max_blocks
                        or self._metadata_tokens > limits.max_tokens
                    ):
                        raise ValueError("Publisher metadata exceeds configured limits")
                except Exception:
                    self._unavailable("metadata_unavailable")
            seq = self._next_seq
            while (
                self._buffer
                and self._replay_bytes + len(payload) > limits.max_reply_bytes
            ):
                self._replay_bytes -= len(self._buffer.popleft()[1])
            if self._buffer.maxlen and len(self._buffer) == self._buffer.maxlen:
                self._replay_bytes -= len(self._buffer[0][1])
            self._buffer.append((seq, payload))
            self._replay_bytes += len(payload)
            self._next_seq = seq + 1
            return seq, payload

        def _build_snapshot_batch(self):
            ordered = self._ordered_metadata()
            snapshot_events = [native.AllBlocksCleared()]
            snapshot_events.extend(block for _, block in ordered)
            ghosts = [
                key for key, _ in ordered if key[1] not in self._group_hashes[key[0]]
            ]
            if ghosts:
                snapshot_events.append(
                    native.BlockRemoved(
                        block_hashes=[key[1] for key in ghosts],
                        medium="GPU",
                        group_idx=0,
                    )
                )
            return native.KVEventBatch(
                ts=time.time(),
                events=snapshot_events,
                data_parallel_rank=self._data_parallel_rank,
            )

        def _build_replay_response(self, request):
            result = {
                "schema_version": 1,
                "publisher_epoch": self._publisher_epoch,
                "next_seq": self._next_seq,
                "replayed_batches": [],
                "snapshot": None,
                "valid": False,
                "reason": self._failure_reason,
            }
            if self._failure_reason is not None:
                return result
            try:
                response = super()._build_replay_response(request)
                result.update(
                    snapshot=response.snapshot,
                    replayed_batches=response.replayed_batches,
                    valid=True,
                )
                if len(self._pack.encode(result)) > limits.max_reply_bytes:
                    raise ValueError("Replay reply exceeds publisher payload limit")
                return result
            except Exception:
                self._unavailable("snapshot_or_reply_unavailable")
                result.update(
                    valid=False,
                    reason=self._failure_reason,
                    snapshot=None,
                    replayed_batches=[],
                    publisher_epoch=self._publisher_epoch,
                    next_seq=self._next_seq,
                )
                return result

        def _publisher_thread(self):
            self._pack = codec.msgpack.Encoder()
            while self._running or self._event_queue.qsize() > 0:
                if self._replay is not None and self._replay.poll(0):
                    try:
                        self._service_replay()
                    except Exception:
                        native.logger.exception("Experimental replay request failed")
                try:
                    event = self._event_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if event is None:
                        break
                    try:
                        seq, payload = self._commit(event)
                    except Exception:
                        self._unavailable("commit_unavailable")
                        native.logger.exception("Experimental publisher commit failed")
                        continue
                    try:
                        self._pub.send_multipart(
                            (
                                self._topic_bytes,
                                seq.to_bytes(8, "big"),
                                payload,
                                self._publisher_epoch.encode("ascii"),
                            )
                        )
                    except Exception:
                        native.logger.exception("PUB failed; committed replay retained")
                finally:
                    self._event_queue.task_done()

    return PrefixRoutingPublisher
