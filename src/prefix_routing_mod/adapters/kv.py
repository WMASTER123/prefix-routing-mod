"""Bounded opt-in HUST frame normalization for the existing native index.

No cache tree or sockets here. The caller must verify instance/model/layout
identity and a consistent recovery cut; normalization does not establish trust.
Token IDs are used transiently for local keys and never emitted in output.
"""

import hashlib
import math

from .profiles import get_profile


def local_block_digest(tokens):
    """Mirror the reused token index key; no engine hash identity is inferred."""
    digest = hashlib.sha256()
    for token in tokens:
        if type(token) is not int or not 0 <= token < 2**32:
            raise ValueError("Invalid token ID")
        digest.update(token.to_bytes(4, "little"))
    return digest.digest()


MAX_PAYLOAD = 1024 * 1024
MAX_OPERATIONS = 4096
MAX_TOKENS = 262_144


def anchor(value):
    if type(value) is int and 0 <= value < 2**64:
        return f"i:{value:016x}"
    if isinstance(value, bytes) and 0 < len(value) <= 32:
        return "b:" + value.hex()
    raise ValueError("unsupported external block hash")


def batch_ops(
    payload,
    decode,
    block_size,
    *,
    profile_id="hust-legacy-publisher-v1",
    max_payload=MAX_PAYLOAD,
    max_operations=MAX_OPERATIONS,
    max_tokens=MAX_TOKENS,
    digest=local_block_digest,
):
    profile = get_profile(profile_id)
    if not isinstance(payload, bytes) or not 0 < len(payload) <= max_payload:
        raise ValueError("payload budget exceeded before decode")
    if type(block_size) is not int or not 0 < block_size <= 4096:
        raise ValueError("unsupported block size")
    if profile.block_size is not None and block_size != profile.block_size:
        raise ValueError("block size outside the audited profile")
    batch = decode(payload)
    if (
        not isinstance(batch, list)
        or len(batch) != 3
        or type(batch[0]) not in (int, float)
        or not math.isfinite(batch[0])
        or type(batch[2]) is not int
        or batch[2] != 0
        or not isinstance(batch[1], list)
        or not 0 < len(batch[1]) <= max_operations
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
            if profile.extra_event_fields and (
                event.get("locality") not in (None, "LOCAL")
                or event.get("ownership") is not None
                or (
                    event.get("session_id") is not None
                    and (
                        not isinstance(event["session_id"], str)
                        or not 0 < len(event["session_id"]) <= 128
                    )
                )
            ):
                raise ValueError("unsupported locality/ownership/session")
            hashes = event.get("block_hashes")
            if not isinstance(hashes, list) or not 0 < len(hashes) <= max_operations:
                raise ValueError("external hash budget exceeded")
            if profile.hash_bytes is not None and any(
                not isinstance(value, bytes) or len(value) != profile.hash_bytes
                for value in hashes
            ):
                raise ValueError("hash outside the audited profile")
            # Check expansion before hashing or creating normalized records.
            if len(result) + len(hashes) > max_operations:
                raise ValueError("normalized operation budget exceeded")
            if kind == "BlockRemoved":
                if set(event) - (
                    {"type", "group_idx", "medium", "block_hashes"}
                    | (set(profile.extra_event_fields) - {"session_id"})
                ):
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
                or event.get("kv_cache_spec_kind") not in profile.cache_spec_kinds
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
            allowed |= set(profile.extra_event_fields)
            if set(event) - allowed:
                raise ValueError("unknown stored-event fields")
            tokens = event.get("token_ids")
            if not isinstance(tokens, list) or len(tokens) != block_size * len(hashes):
                raise ValueError("incomplete token blocks")
            tokens_seen += len(tokens)
            if tokens_seen > max_tokens:
                raise ValueError("batch token budget exceeded")
            extras = event.get("extra_keys")
            if extras is not None and (
                not isinstance(extras, list)
                or len(extras) != len(hashes)
                or any(item is not None for item in extras)
            ):
                raise ValueError("additional keys need a separate domain")
            parent = event.get("parent_block_hash")
            if (
                parent is not None
                and profile.hash_bytes is not None
                and (not isinstance(parent, bytes) or len(parent) != profile.hash_bytes)
            ):
                raise ValueError("parent hash outside the audited profile")
            parent = None if parent is None else anchor(parent)
            for offset, block_hash in enumerate(hashes):
                current = anchor(block_hash)
                key = digest(
                    tokens[offset * block_size : (offset + 1) * block_size]
                ).hex()
                result.append(
                    {"kind": "store", "id": current, "parent": parent, "local": key}
                )
                parent = current
        else:
            raise ValueError("unsupported event kind")
        if len(result) > max_operations:
            raise ValueError("normalized operation budget exceeded")
    return result


def native_live_batch(frames, decode, *, profile_id, block_size, topic=b""):
    """Normalize one frame set, without claiming epoch, coverage or residency."""
    profile = get_profile(profile_id)
    if profile.wire_protocol != "native-sequence-v1":
        raise ValueError("profile does not use native sequence framing")
    if (
        not isinstance(frames, (list, tuple))
        or len(frames) != 3
        or any(not isinstance(frame, bytes) for frame in frames)
        or frames[0] != topic
        or len(frames[1]) != 8
        or frames[1] == b"\xff" * 8
    ):
        raise ValueError("invalid native framing")
    return {
        "sequence": int.from_bytes(frames[1], "big"),
        "ops": batch_ops(frames[2], decode, block_size, profile_id=profile_id),
    }


def native_replay_batch(frames, decode, *, profile_id, block_size, topic=b""):
    """DEALER delimiter is part of this profile. END_SEQ is not a snapshot."""
    if (
        not isinstance(frames, (list, tuple))
        or len(frames) != 4
        or any(not isinstance(frame, bytes) for frame in frames)
        or frames[0] != b""
    ):
        raise ValueError("invalid native replay framing")
    if get_profile(profile_id).wire_protocol != "native-sequence-v1":
        raise ValueError("profile does not use native sequence framing")
    if tuple(frames[1:]) == (b"", b"\xff" * 8, b""):
        return None
    return native_live_batch(
        frames[1:], decode, profile_id=profile_id, block_size=block_size, topic=topic
    )
