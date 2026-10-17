from copy import deepcopy

import pytest

from prefix_routing_mod.adapters import kv

PROFILE = "hust-cedb477-native-v1"


def event():
    return {
        "type": "BlockStored",
        "block_hashes": [b"a" * 32, b"b" * 32],
        "parent_block_hash": None,
        "token_ids": list(range(256)),
        "block_size": 128,
        "lora_id": None,
        "lora_name": None,
        "medium": "GPU",
        "group_idx": 0,
        "kv_cache_spec_kind": "full_attention",
        "locality": "LOCAL",
        "ownership": None,
        "session_id": "context-only",
    }


def normalize(value):
    return kv.batch_ops(b"payload", lambda _: [0, [value], 0], 128, profile_id=PROFILE)


def test_new_profile_produces_same_index_input_shape_without_session_or_tokens():
    ops = normalize(event())
    assert ops[1]["parent"] == ops[0]["id"]
    assert ops[0]["local"] == kv.local_block_digest(range(128)).hex()
    assert all(set(op) == {"kind", "id", "parent", "local"} for op in ops)


@pytest.mark.parametrize(
    "change",
    [
        {"locality": "REMOTE"},
        {"ownership": "other-tier"},
        {"group_idx": 1},
        {"medium": "CPU"},
        {"block_hashes": [1, 2]},
        {"parent_block_hash": 1},
        {"block_size": 16},
        {"kv_cache_spec_kind": "FullAttentionSpec"},
        {"kv_cache_spec_kind": None},
        {"session_id": False},
        {"unknown": 1},
        {"extra_keys": [["salt"], None]},
    ],
)
def test_new_identity_semantics_fail_closed(change):
    with pytest.raises(ValueError):
        normalize(event() | change)


def test_profiles_do_not_accept_each_others_event_semantics():
    with pytest.raises(ValueError):
        kv.batch_ops(b"payload", lambda _: [0, [event()], 0], 128)


def test_native_live_replay_and_end_have_distinct_meaning():
    live = [b"", (2).to_bytes(8, "big"), b"payload"]

    def decode(_):
        return [0, [event()], 0]

    options = dict(profile_id=PROFILE, block_size=128)
    a = kv.native_live_batch(live, decode, **options)
    b = kv.native_replay_batch([b"", *live], decode, **options)
    assert a == b and a["sequence"] == 2
    assert (
        kv.native_replay_batch([b"", b"", b"\xff" * 8, b""], decode, **options) is None
    )
    assert set(a) == {"sequence", "ops"}
    with pytest.raises(ValueError):
        kv.native_live_batch([b"", b"\xff" * 8, b""], decode, **options)


@pytest.mark.parametrize(
    "frames",
    [
        [b"payload"],
        [b"bad", b"", b"\xff" * 8, b""],
        [b"", b"", b"\xff" * 8, b"not-end"],
        [b"", b"wrong", b"short", b"payload"],
    ],
)
def test_bad_replay_does_not_create_snapshot(frames):
    with pytest.raises(ValueError):
        kv.native_replay_batch(
            frames, lambda _: None, profile_id=PROFILE, block_size=128
        )


def test_decoder_never_called_for_oversized_frame_or_old_protocol():
    calls = []
    frames = [b"", b"\0" * 8, b"x" * (kv.MAX_PAYLOAD + 1)]
    with pytest.raises(ValueError, match="before decode"):
        kv.native_live_batch(frames, calls.append, profile_id=PROFILE, block_size=128)
    assert not calls
    with pytest.raises(ValueError, match="profile"):
        kv.native_live_batch(
            frames, calls.append, profile_id="hust-legacy-publisher-v1", block_size=128
        )


def test_missing_identity_and_bad_remove_are_rejected():
    value = deepcopy(event())
    del value["parent_block_hash"]
    with pytest.raises(ValueError, match="missing"):
        normalize(value)
    with pytest.raises(ValueError):
        normalize(
            {
                "type": "BlockRemoved",
                "block_hashes": [b"a" * 32],
                "medium": "GPU",
                "group_idx": 0,
                "session_id": "unexpected",
            }
        )
