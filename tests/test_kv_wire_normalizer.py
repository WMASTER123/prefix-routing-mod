"""Protocol failure checks without installing codec/device dependencies."""

from copy import deepcopy

import pytest

from tools import kv_wire_normalizer as wire


def stored():
    return {
        "type": "BlockStored",
        "block_hashes": [11, 12],
        "parent_block_hash": None,
        "token_ids": [1, 2, 3, 4],
        "block_size": 2,
        "group_idx": 0,
        "medium": "GPU",
        "lora_id": None,
        "lora_name": None,
    }


def decode(_):
    return [0.0, [stored()], 0]


def frames():
    return [b"", (7).to_bytes(8, "big"), b"payload", b"1" * 32]


def test_normalized_chain_has_no_token_payload():
    result = wire.live_batch(frames(), decode, publisher_epoch="1" * 32, block_size=2)
    assert result["sequence"] == 7
    assert result["ops"][0]["parent"] is None
    assert result["ops"][1]["parent"] == result["ops"][0]["id"]
    assert all(len(op["local"]) == 64 and "token_ids" not in op for op in result["ops"])


def test_payload_limit_before_decoder():
    calls = []
    with pytest.raises(ValueError, match="before decode"):
        wire.batch_ops(b"x" * (wire.MAX_PAYLOAD + 1), calls.append, 2)
    assert not calls


@pytest.mark.parametrize(
    "change",
    [
        {"group_idx": 1},
        {"group_idx": False},
        {"medium": "CPU"},
        {"lora_name": "adapter"},
        {"block_size": True},
        {"token_ids": []},
        {"token_ids": [1, True, 3, 4]},
        {"extra_keys": [["salt"], None]},
        {"parent_block_hash": -1},
        {"block_hashes": [True, 12]},
        {"unknown": 1},
        {"kv_cache_spec_sliding_window": 1024},
    ],
)
def test_reject_unsupported_or_incomplete_identity(change):
    value = stored() | change
    with pytest.raises(ValueError):
        wire.batch_ops(b"payload", lambda _: [0.0, [value], 0], 2)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f[:3],
        lambda f: [b"wrong", *f[1:]],
        lambda f: [f[0], b"short", *f[2:]],
        lambda f: [*f[:3], b"2" * 32],
    ],
)
def test_reject_bad_frame_or_old_epoch(mutate):
    with pytest.raises(ValueError):
        wire.live_batch(
            mutate(frames()), decode, publisher_epoch="1" * 32, block_size=2
        )


@pytest.mark.parametrize(
    "batch",
    [
        [0, [], 0],
        [0, [stored()], False],
        [float("nan"), [stored()], 0],
        [0, [stored()]],
        [0, [{"type": "Unknown"}], 0],
    ],
)
def test_bad_batch_envelope(batch):
    with pytest.raises(ValueError):
        wire.batch_ops(b"payload", lambda _: batch, 2)


def test_hash_encoding_keeps_int_and_bytes_domains_separate():
    assert wire.anchor(1) != wire.anchor(b"\x01")


@pytest.mark.parametrize("field", ["parent_block_hash", "lora_id", "lora_name"])
def test_missing_metadata_cannot_be_interpreted_as_none(field):
    value = stored()
    del value[field]
    with pytest.raises(ValueError, match="missing required"):
        wire.batch_ops(b"payload", lambda _: [0, [value], 0], 2)


def test_operation_expansion_checked_before_processing(monkeypatch):
    monkeypatch.setattr(wire, "MAX_OPERATIONS", 3)
    value = deepcopy(stored())
    value["block_hashes"] = [1, 2]
    calls = []
    monkeypatch.setattr(wire, "local_block_digest", lambda tokens: calls.append(tokens))
    # Two removal ops then two stores would exceed the normalized budget.
    remove = {
        "type": "BlockRemoved",
        "block_hashes": [3, 4],
        "group_idx": 0,
        "medium": "GPU",
    }
    with pytest.raises(ValueError, match="operation budget"):
        wire.batch_ops(b"payload", lambda _: [0, [remove, value], 0], 2)
    assert not calls
