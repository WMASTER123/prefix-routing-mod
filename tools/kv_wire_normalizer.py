"""Bounded opt-in HUST frame normalization for the existing native index.

No cache tree or sockets here. The caller must verify instance/model/layout
identity and a consistent recovery cut; normalization does not establish trust.
Token IDs are used transiently for local keys and never emitted in output.
"""

import math

from tools.kv_identity import local_block_digest

MAX_PAYLOAD = 1024 * 1024
MAX_OPERATIONS = 4096
MAX_TOKENS = 262_144


def anchor(value):
    if type(value) is int and 0 <= value < 2**64:
        return f"i:{value:016x}"
    if isinstance(value, bytes) and 0 < len(value) <= 32:
        return "b:" + value.hex()
    raise ValueError("unsupported external block hash")


def batch_ops(payload, decode, block_size):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_PAYLOAD:
        raise ValueError("payload budget exceeded before decode")
    if type(block_size) is not int or not 0 < block_size <= 4096:
        raise ValueError("unsupported block size")
    batch = decode(payload)
    if (
        not isinstance(batch, list)
        or len(batch) != 3
        or type(batch[0]) not in (int, float)
        or not math.isfinite(batch[0])
        or type(batch[2]) is not int
        or batch[2] != 0
        or not isinstance(batch[1], list)
        or not 0 < len(batch[1]) <= MAX_OPERATIONS
    ):
        raise ValueError("unsupported batch envelope")
    result = []
    tokens_seen = 0
    for event in batch[1]:
        if not isinstance(event, dict):
            raise ValueError("event must be tagged object")
        kind = event.get("type")
        if kind == "AllBlocksCleared":
            if set(event) != {"type"}:
                raise ValueError("unexpected clear fields")
            result.append({"kind": "clear"})
        elif kind in ("BlockStored", "BlockRemoved"):
            if (
                type(event.get("group_idx")) is not int
                or event["group_idx"] != 0
                or event.get("medium") != "GPU"
            ):
                raise ValueError("unsupported group/tier")
            hashes = event.get("block_hashes")
            if not isinstance(hashes, list) or not 0 < len(hashes) <= MAX_OPERATIONS:
                raise ValueError("external hash budget exceeded")
            # Check expansion before hashing or creating normalized records.
            if len(result) + len(hashes) > MAX_OPERATIONS:
                raise ValueError("normalized operation budget exceeded")
            if kind == "BlockRemoved":
                if set(event) - {"type", "group_idx", "medium", "block_hashes"}:
                    raise ValueError("unexpected removal fields")
                result.extend({"kind": "remove", "id": anchor(h)} for h in hashes)
                continue
            required = {
                "type",
                "block_hashes",
                "parent_block_hash",
                "token_ids",
                "block_size",
                "lora_id",
                "medium",
                "lora_name",
                "group_idx",
            }
            if not required.issubset(event):
                raise ValueError("missing required stored metadata")
            if (
                event.get("block_size") != block_size
                or type(event.get("block_size")) is not int
                or event.get("lora_id") is not None
                or event.get("lora_name") is not None
                or event.get("kv_cache_spec_sliding_window") is not None
                or event.get("kv_cache_spec_kind") not in (None, "FullAttentionSpec")
            ):
                raise ValueError("unsupported cache identity/layout")
            allowed = {
                "type",
                "block_hashes",
                "parent_block_hash",
                "token_ids",
                "block_size",
                "lora_id",
                "medium",
                "lora_name",
                "extra_keys",
                "group_idx",
                "kv_cache_spec_kind",
                "kv_cache_spec_sliding_window",
            }
            if set(event) - allowed:
                raise ValueError("unknown stored-event fields")
            tokens = event.get("token_ids")
            if not isinstance(tokens, list) or len(tokens) != block_size * len(hashes):
                raise ValueError("incomplete token blocks")
            tokens_seen += len(tokens)
            if tokens_seen > MAX_TOKENS:
                raise ValueError("batch token budget exceeded")
            extras = event.get("extra_keys")
            if extras is not None and (
                not isinstance(extras, list)
                or len(extras) != len(hashes)
                or any(item is not None for item in extras)
            ):
                raise ValueError("additional keys need a separate domain")
            parent = event.get("parent_block_hash")
            parent = None if parent is None else anchor(parent)
            for offset, block_hash in enumerate(hashes):
                current = anchor(block_hash)
                key = local_block_digest(
                    tokens[offset * block_size : (offset + 1) * block_size]
                ).hex()
                result.append(
                    {"kind": "store", "id": current, "parent": parent, "local": key}
                )
                parent = current
        else:
            raise ValueError("unsupported event kind")
        if len(result) > MAX_OPERATIONS:
            raise ValueError("normalized operation budget exceeded")
    return result


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
