"""Bounded request/event correspondence checks, without a residency index."""

import hashlib


def local_block_digest(tokens):
    """Mirror the pinned Router's plain-text local key (u32 little endian)."""
    digest = hashlib.sha256()
    for token in tokens:
        if type(token) is not int or not 0 <= token < 2**32:
            raise ValueError("Invalid token ID")
        digest.update(token.to_bytes(4, "little"))
    return digest.digest()


def check_stored_chain(event, request_tokens, anchors, *, hash_function=None):
    """Validate full blocks of one plain-text request; return private anchors.

    Anchors describe positions in this controlled request, not live residency.
    The caller owns their lifetime and must not publish their raw contents.
    """
    size = event.get("block_size")
    hashes, tokens = event.get("block_hashes"), event.get("token_ids")
    if (
        event.get("type") != "BlockStored"
        or type(size) is not int
        or size <= 0
        or not isinstance(hashes, list)
        or not hashes
        or len(hashes) > 1024
        or not isinstance(tokens, list)
        or len(tokens) != size * len(hashes)
        or len(tokens) > 16384
    ):
        raise ValueError("Incomplete full-block token metadata")
    if event.get("group_idx") != 0 or event.get("medium") != "GPU":
        raise ValueError("Probe requires explicit group 0 device events")
    if event.get("lora_id") is not None or event.get("lora_name") is not None:
        raise ValueError("LoRA identity is outside this probe")
    extras = event.get("extra_keys")
    if extras is not None and (
        not isinstance(extras, list)
        or len(extras) != len(hashes)
        or any(item is not None for item in extras)
    ):
        raise ValueError("Additional cache keys require a separate identity domain")
    for value in hashes:
        if not (
            (isinstance(value, bytes) and 0 < len(value) <= 64)
            or (type(value) is int and 0 <= value < 2**64)
        ):
            raise ValueError("Invalid external block hash")
    parent = event.get("parent_block_hash")
    if parent is None:
        start = 0
    else:
        if (
            not (isinstance(parent, bytes) or type(parent) is int)
            or parent not in anchors
        ):
            raise ValueError("Parent position is unknown")
        start = anchors[parent]
    if tokens != request_tokens[start : start + len(tokens)]:
        raise ValueError("Stored tokens differ from the submitted request")
    updated = dict(anchors)
    local_keys = []
    native_checks = 0
    for offset, current_hash in enumerate(hashes):
        chunk = tokens[offset * size : (offset + 1) * size]
        local_keys.append(local_block_digest(chunk))
        if hash_function is not None and isinstance(parent, bytes):
            if current_hash != hash_function((parent, tuple(chunk), None)):
                raise ValueError("Native chained hash differs")
            native_checks += 1
        position = start + (offset + 1) * size
        if current_hash in updated and updated[current_hash] != position:
            raise ValueError("Conflicting hash position")
        updated[current_hash] = position
        parent = current_hash
    if len(updated) > 1024:
        raise ValueError("Controlled anchor bound exceeded")
    return updated, {
        "blocks": len(hashes),
        "tokens": len(tokens),
        "block_size": size,
        "group_idx": 0,
        "parent_known": event.get("parent_block_hash") is not None,
        "native_chained_hash_checks": native_checks,
        "external_hash_kind": "bytes" if isinstance(hashes[0], bytes) else "int",
        "local_keys_match_request": all(
            key == local_block_digest(request_tokens[i : i + size])
            for key, i in zip(
                local_keys, range(start, start + len(tokens), size), strict=True
            )
        ),
    }
