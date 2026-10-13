"""Legacy opt-in probe API; normalization is shared with packaged adapters."""

from prefix_routing_mod.adapters import kv as _kv
from prefix_routing_mod.adapters.kv import anchor, local_block_digest  # noqa: F401

MAX_PAYLOAD = _kv.MAX_PAYLOAD
MAX_OPERATIONS = _kv.MAX_OPERATIONS
MAX_TOKENS = _kv.MAX_TOKENS


def batch_ops(payload, decode, block_size):
    return _kv.batch_ops(
        payload,
        decode,
        block_size,
        max_payload=MAX_PAYLOAD,
        max_operations=MAX_OPERATIONS,
        max_tokens=MAX_TOKENS,
        digest=local_block_digest,
    )


def live_batch(frames, decode, *, publisher_epoch, block_size, topic=b""):
    if (
        not isinstance(frames, (list, tuple))
        or len(frames) != 4
        or any(not isinstance(frame, bytes) for frame in frames)
        or frames[0] != topic
        or len(frames[1]) != 8
        or len(frames[3]) != 32
    ):
        raise ValueError("invalid opt-in framing")
    epoch = frames[3].decode("ascii")
    if any(c not in "0123456789abcdef" for c in epoch) or epoch != publisher_epoch:
        raise ValueError("publisher epoch mismatch")
    return {
        "sequence": int.from_bytes(frames[1], "big"),
        "ops": batch_ops(frames[2], decode, block_size),
    }
