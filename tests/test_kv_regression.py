import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import regress_kv_events


def response(**changes):
    return {
        "publisher_epoch": "epoch",
        "next_seq": 3,
        "replayed_batches": [[1, b"one"], [2, b"two"]],
        **changes,
    }


def test_complete_replay_covers_exact_range():
    value = response()
    assert regress_kv_events.validate_response(value, 1, "epoch") is value


@pytest.mark.parametrize(
    "changes",
    [
        {"publisher_epoch": "different"},
        {"publisher_epoch": None},
        {"next_seq": True},
        {"next_seq": -1},
        {"next_seq": 0},
        {"replayed_batches": [[2, b"two"]]},
        {"replayed_batches": [[1, b"one"], [1, b"duplicate"]]},
        {"replayed_batches": [[1, b"one"], [2, "not-bytes"]]},
        {"snapshot": b"snapshot-with-replay"},
        {"snapshot": "not-bytes"},
    ],
)
def test_incomplete_or_malformed_recovery_is_rejected(changes):
    with pytest.raises(ValueError):
        regress_kv_events.validate_response(response(**changes), 1, "epoch")


def test_new_epoch_requires_snapshot_not_empty_replay():
    value = response(
        publisher_epoch="new", next_seq=0, replayed_batches=[], snapshot=b"snapshot"
    )
    assert regress_kv_events.validate_response(value, 99, "old") is value
    value.pop("snapshot")
    with pytest.raises(ValueError):
        regress_kv_events.validate_response(value, 0, "old")


def test_replay_request_closes_only_its_client_even_on_receive_error():
    context, zmq, msgspec = Mock(), Mock(), Mock()
    client = context.socket.return_value
    client.recv.side_effect = TimeoutError("synthetic receive timeout")
    with pytest.raises(TimeoutError):
        regress_kv_events.replay(context, zmq, msgspec, "ipc:///private/replay", 0)
    client.close.assert_called_once_with(linger=0)
    context.term.assert_not_called()


def test_live_gap_is_rejected_before_any_summary_is_applied():
    subscriber = Mock()
    subscriber.poll.return_value = True
    subscriber.recv_multipart.return_value = [b"", (7).to_bytes(8, "big"), b"payload"]
    receiver = regress_kv_events.TestReceiver(subscriber, Mock())
    receiver.next_seq = 5
    with pytest.raises(ValueError, match="continuity"):
        receiver.collect()
    assert receiver.batches == []


def test_pre_snapshot_frames_are_discarded_before_applying_live_events():
    subscriber = Mock()
    subscriber.poll.side_effect = [True, False]
    subscriber.recv_multipart.return_value = [b"", (0).to_bytes(8, "big"), b"old"]
    msgspec = Mock()
    receiver = regress_kv_events.TestReceiver(subscriber, msgspec)
    receiver.floor_seq = receiver.next_seq = 1
    receiver.collect()
    msgspec.msgpack.decode.assert_not_called()
    assert receiver.next_seq == 1 and receiver.batches == []


def test_summary_does_not_persist_token_ids_or_block_hashes():
    subscriber = Mock()
    subscriber.poll.side_effect = [True, False]
    subscriber.recv_multipart.return_value = [b"", (0).to_bytes(8, "big"), b"payload"]
    batch = [
        1.0,
        [
            {
                "type": "BlockStored",
                "block_hashes": ["private-hash"],
                "parent_block_hash": None,
                "token_ids": [99],
                "block_size": 1,
                "medium": "GPU",
            }
        ],
    ]
    msgspec = SimpleNamespace(msgpack=SimpleNamespace(decode=lambda value: batch))
    receiver = regress_kv_events.TestReceiver(subscriber, msgspec)
    receiver.next_seq = 0
    receiver.collect(required_kind="BlockStored")
    serialized = json.dumps(receiver.batches)
    assert "private-hash" not in serialized and "token_ids" not in serialized
    assert receiver.counts["BlockStored"] == 1


def test_eviction_configuration_is_separate_from_performance_baseline():
    command = regress_kv_events.test_command(
        Path("/bin/vllm"), Path("/models/qwen"), 18230, PurePosixPath("/private/probe")
    )
    assert command[command.index("--max-model-len") + 1] == "1024"
    assert command[command.index("--kv-cache-memory-bytes") + 1] == str(1024**3)
    config = json.loads(command[command.index("--kv-events-config") + 1])
    assert config["buffer_steps"] == 4
    assert config["replay_endpoint"] == "ipc:///private/probe/replay.sock"
    assert command[command.index("--host") + 1] == "127.0.0.1"


def test_reset_dev_api_is_enabled_only_in_private_test_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VLLM_SERVER_DEV_MODE", "0")
    env = regress_kv_events.test_environment(0, tmp_path)
    assert env["VLLM_SERVER_DEV_MODE"] == "1"
    assert regress_kv_events.smoke_backend.os.environ["VLLM_SERVER_DEV_MODE"] == "0"
