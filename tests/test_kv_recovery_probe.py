import json
import sys
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import regress_kv_recovery as probe
from tools.kv_recovery_cursor import RecoveryCursor


def test_restart_uses_same_endpoints_and_separate_cache_directories():
    endpoint = PurePosixPath("/private/run")
    configs = []
    for name in ("a", "b"):
        command = probe.recovery_command(
            PurePosixPath("/venv/bin/vllm"),
            PurePosixPath("/models/qwen"),
            18242,
            endpoint / name,
            endpoint,
        )
        configs.append(json.loads(command[command.index("--kv-events-config") + 1]))
    assert configs[0] == configs[1]
    assert configs[0]["endpoint"] == "ipc:///private/run/events.sock"
    assert configs[0]["replay_endpoint"] == "ipc:///private/run/replay.sock"
    assert configs[0]["buffer_steps"] == 4


def test_decoding_failure_does_not_advance_live_cursor():
    subscriber, msgspec = Mock(), Mock()
    subscriber.poll.return_value = True
    subscriber.recv_multipart.return_value = [b"", (0).to_bytes(8, "big"), b"bad"]
    msgspec.msgpack.decode.side_effect = ValueError("invalid message")
    cursor = RecoveryCursor()
    cursor.start_supervised_empty("epoch", 0, now=0)
    with pytest.raises(ValueError, match="invalid message"):
        probe.collect_live(subscriber, cursor, msgspec, 0, count=1)
    assert cursor.next_seq == 0 and not cursor.recent
    assert not cursor.continuous


def test_all_replay_payloads_are_decoded_before_advancing_cursor():
    cursor = RecoveryCursor()
    cursor.start_supervised_empty("epoch", 0, now=0)
    msgspec = Mock()
    msgspec.msgpack.decode.side_effect = [
        [0, [{"type": "AllBlocksCleared"}]],
        ValueError("bad second batch"),
    ]
    response = {
        "publisher_epoch": "epoch",
        "next_seq": 2,
        "replayed_batches": [[0, b"valid"], [1, b"invalid"]],
    }
    with pytest.raises(ValueError, match="bad second batch"):
        probe.checked_recovery(response, cursor, msgspec)
    assert cursor.next_seq == 0 and not cursor.recent
    assert not cursor.continuous


def test_start_failure_closes_subscriber_context_and_owned_backend(
    tmp_path, monkeypatch
):
    context, subscriber = Mock(), Mock()
    monkeypatch.setitem(sys.modules, "msgspec", Mock())
    zmq = Mock()
    zmq.Context.return_value = context
    monkeypatch.setitem(sys.modules, "zmq", zmq)
    monkeypatch.setattr(probe.smoke_backend, "ipc_endpoint", lambda value: str(value))
    monkeypatch.setattr(
        probe.smoke_backend, "create_event_subscriber", lambda *a: subscriber
    )
    monkeypatch.setattr(probe, "recovery_command", lambda *a: ["owned-command"])
    monkeypatch.setattr(probe.events, "test_environment", lambda *a: {})
    monkeypatch.setattr(
        probe.OwnedBackend, "start", Mock(side_effect=RuntimeError("startup"))
    )
    cleanup = Mock()
    monkeypatch.setattr(probe.OwnedBackend, "stop", cleanup)
    args = SimpleNamespace(port=18242, device=0, executable="vllm", model="qwen")
    report = {"checks": {}}
    with pytest.raises(RuntimeError, match="startup"):
        probe.run_cases(args, tmp_path, report)
    cleanup.assert_called_once()
    subscriber.close.assert_called_once_with(linger=0)
    context.term.assert_called_once()
    assert len(report["backends"]) == 1


def test_owned_cleanup_is_idempotent_and_never_enumerates_other_processes(
    tmp_path, monkeypatch
):
    backend = probe.OwnedBackend([], {}, tmp_path, Mock(), "http://127.0.0.1")
    backend.process = Mock()
    stop = Mock(return_value={"parent_returncode": 0})
    monkeypatch.setattr(probe.smoke_backend, "stop_owned_group", stop)
    backend.stop()
    backend.stop()
    stop.assert_called_once_with(backend.process)


def test_live_summary_has_no_prompt_tokens_or_block_hashes():
    subscriber = Mock()
    subscriber.poll.return_value = True
    subscriber.recv_multipart.return_value = [b"", (0).to_bytes(8, "big"), b"encoded"]
    msgspec = Mock()
    msgspec.msgpack.decode.return_value = [
        0,
        [
            {
                "type": "BlockStored",
                "block_hashes": ["private-block"],
                "parent_block_hash": None,
                "token_ids": [12345],
                "block_size": 1,
                "medium": "GPU",
            }
        ],
    ]
    cursor = RecoveryCursor()
    cursor.start_supervised_empty("epoch", 0, now=0)
    result = probe.collect_live(subscriber, cursor, msgspec, 0, count=1)
    serialized = json.dumps(result)
    assert result["accepted"] == 1
    assert "private-block" not in serialized and "12345" not in serialized
