from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import hust_publisher_probe as probe


def codec(events):
    return SimpleNamespace(
        msgpack=SimpleNamespace(decode=Mock(return_value=[0, events, 0]))
    )


def stored(hashes, tokens, parent=None):
    return {
        "type": "BlockStored",
        "block_hashes": hashes,
        "token_ids": tokens,
        "parent_block_hash": parent,
        "block_size": 2,
        "group_idx": 0,
        "medium": "GPU",
    }


def test_snapshot_stages_topology_then_removes_ghost_claim():
    catalog, residents = {}, set()
    probe.apply_batch(
        b"encoded",
        codec(
            [
                {"type": "AllBlocksCleared"},
                stored([1], [10, 11]),
                stored([2], [12, 13], 1),
                {
                    "type": "BlockRemoved",
                    "block_hashes": [1],
                    "group_idx": 0,
                    "medium": "GPU",
                },
            ]
        ),
        catalog,
        residents,
    )
    assert residents == {(0, 2)} and len(catalog) == 2
    assert catalog[(0, 2)] == (1, (12, 13))


@pytest.mark.parametrize(
    "event",
    [
        stored([1], []),
        stored([2], [1, 2], 1),
        stored([1], [1, 2]) | {"extra_keys": [["salt"]]},
    ],
)
def test_missing_metadata_never_becomes_a_residency_claim(event):
    catalog, residents = {}, set()
    with pytest.raises(ValueError):
        probe.apply_batch(b"encoded", codec([event]), catalog, residents)
    assert not residents


@pytest.mark.parametrize(
    "frames",
    [
        [b"", b"12345678", b"payload"],
        [b"", b"short", b"payload", b"a" * 32],
        [b"", b"12345678", b"payload", b"not-an-epoch"],
    ],
)
def test_native_or_malformed_frames_rejected(frames):
    with pytest.raises(ValueError):
        probe.live_epoch(frames)


def test_epoch_is_read_from_wire_and_unavailable_response_rejected():
    assert probe.live_epoch([b"", b"12345678", b"payload", b"a" * 32]) == "a" * 32
    with pytest.raises(ValueError, match="unavailable"):
        probe.check_response({"schema_version": 1, "valid": False}, 0, "a" * 32)
