import hashlib
import json

import pytest

from tools.kv_identity import check_stored_chain, local_block_digest


def stored(**kwargs):
    return {
        "type": "BlockStored",
        "block_hashes": [b"a", b"b"],
        "token_ids": [1, 2, 3, 4],
        "block_size": 2,
        "parent_block_hash": None,
        "group_idx": 0,
        "medium": "GPU",
        **kwargs,
    }


def test_plain_local_key_matches_pinned_router_u32_encoding():
    assert (
        local_block_digest([1, 256])
        == hashlib.sha256(b"\x01\x00\x00\x00\x00\x01\x00\x00").digest()
    )


def test_parent_offsets_and_private_summary():
    anchors, first = check_stored_chain(stored(), [1, 2, 3, 4], {})
    extended = stored(
        block_hashes=[b"private"], token_ids=[5, 6], parent_block_hash=b"b"
    )
    updated, result = check_stored_chain(extended, [1, 2, 3, 4, 5, 6], anchors)
    assert updated[b"private"] == 6 and result["parent_known"]
    assert first["blocks"] == 2 and result["local_keys_match_request"]
    assert "private" not in json.dumps(result)
    assert b"private" not in anchors


@pytest.mark.parametrize(
    "mutation",
    [
        {"token_ids": []},
        {"token_ids": [1, 2, 3]},
        {"token_ids": [9, 2, 3, 4]},
        {"parent_block_hash": b"unknown"},
        {"group_idx": None},
        {"group_idx": 1},
        {"medium": "CPU"},
        {"block_size": True},
        {"lora_name": "adapter"},
        {"extra_keys": [["salt"], None]},
        {"extra_keys": []},
        {"block_hashes": [-1, 2]},
        {"block_hashes": [True, 2]},
    ],
)
def test_incompatible_metadata_rejected_without_changing_anchors(mutation):
    anchors = {b"old": 128}
    with pytest.raises(ValueError):
        check_stored_chain(stored(**mutation), [1, 2, 3, 4], anchors)
    assert anchors == {b"old": 128}


def test_native_chained_hash_checks_observed_parent_not_foreign_seed():
    def hash_function(value):
        return hashlib.sha256(repr(value).encode()).digest()

    second = hash_function((b"first", (3, 4), None))
    anchors, result = check_stored_chain(
        stored(block_hashes=[b"first", second]),
        [1, 2, 3, 4],
        {},
        hash_function=hash_function,
    )
    assert result["native_chained_hash_checks"] == 1
    assert anchors[second] == 4
    with pytest.raises(ValueError, match="Native chained"):
        check_stored_chain(stored(), [1, 2, 3, 4], {}, hash_function=hash_function)


@pytest.mark.parametrize("tokens", [[True], [-1], [2**32], ["1"]])
def test_invalid_tokens_rejected(tokens):
    with pytest.raises(ValueError, match="token ID"):
        local_block_digest(tokens)
