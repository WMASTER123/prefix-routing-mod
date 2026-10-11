import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest

from tools import smoke_backend


def frame(content):
    return b"data: " + json.dumps(content).encode() + b"\n"


def test_command_is_loopback_single_replica_and_disables_host_irq_binding():
    command = smoke_backend.build_command(
        Path("/bin/vllm"), Path("/models/qwen"), 18140
    )
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--tensor-parallel-size") + 1] == "1"
    assert json.loads(command[command.index("--additional-config") + 1]) == {
        "enable_cpu_binding": False
    }
    assert "--enforce-eager" in command  # Functional probe, not a performance baseline.
    assert "--enable-prefix-caching" in command
    assert "--trust-remote-code" not in command
    assert "ascend" in smoke_backend.PLUGINS.split(",")
    assert "msserviceprofiler" not in smoke_backend.PLUGINS.split(",")


def test_stream_requires_content_finish_and_done():
    result = smoke_backend.parse_stream(
        [
            b": heartbeat\n",
            frame({"choices": [{"text": "hello", "finish_reason": None}]}),
            frame({"choices": [{"text": "", "finish_reason": "length"}]}),
            b"data: [DONE]\n",
        ]
    )
    assert result == {"content_chunks": 1, "content_chars": 5, "done": True}


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [b"data: [DONE]\n"],
        [frame({"error": "backend failure"})],
        [frame({"choices": [{"text": "hello", "finish_reason": "length"}]})],
        [frame({"choices": [{"text": "hello"}]}), b"data: [DONE]\n"],
    ],
)
def test_incomplete_or_error_stream_fails(lines):
    with pytest.raises(ValueError):
        smoke_backend.parse_stream(lines)


def test_cleanup_only_signals_owned_group_including_survivors(monkeypatch):
    calls = []
    monkeypatch.setattr(
        smoke_backend.os, "killpg", lambda *args: calls.append(args), raising=False
    )
    monkeypatch.setattr(smoke_backend.signal, "SIGKILL", 9, raising=False)
    process = Mock(pid=12345, returncode=0)
    process.poll.return_value = None
    result = smoke_backend.stop_owned_group(process)
    assert calls == [(12345, 0), (12345, 9)]
    assert result == {"parent_returncode": 0, "forced_group_cleanup": True}
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=20)


def test_cleanup_timeout_escalates_only_owned_group(monkeypatch):
    calls = []
    monkeypatch.setattr(
        smoke_backend.os, "killpg", lambda *args: calls.append(args), raising=False
    )
    monkeypatch.setattr(smoke_backend.signal, "SIGKILL", 9, raising=False)
    process = Mock(pid=12345, returncode=-9)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("probe", 20), 0]
    smoke_backend.stop_owned_group(process)
    assert all(pid == 12345 for pid, _ in calls)
    assert calls[-1][1] == 9


def test_clean_exit_does_not_force_cleanup(monkeypatch):
    def missing_group(pid, sig):
        assert (pid, sig) == (12345, 0)
        raise ProcessLookupError

    monkeypatch.setattr(smoke_backend.os, "killpg", missing_group, raising=False)
    process = Mock(pid=12345, returncode=0)
    process.poll.return_value = 0
    assert smoke_backend.stop_owned_group(process)["forced_group_cleanup"] is False
    process.terminate.assert_not_called()


def test_events_endpoint_and_queues_are_bounded():
    command = smoke_backend.build_command(
        Path("/bin/vllm"), Path("/models/qwen"), 18140, 18141
    )
    config = json.loads(command[command.index("--kv-events-config") + 1])
    assert config["endpoint"] == "tcp://127.0.0.1:18141"
    assert config["hwm"] == config["max_queue_size"] == config["buffer_steps"] == 128


def test_subscriber_binds_loopback_to_match_upstream_connect():
    context, zmq = Mock(), Mock()
    subscriber = smoke_backend.create_event_subscriber(context, zmq, 18141)
    subscriber.bind.assert_called_once_with("tcp://127.0.0.1:18141")
    subscriber.connect.assert_not_called()
    subscriber.setsockopt.assert_any_call(zmq.RCVHWM, 128)


def test_subscriber_bind_failure_closes_owned_socket():
    context, zmq = Mock(), Mock()
    context.socket.return_value.bind.side_effect = OSError("port in use")
    with pytest.raises(OSError):
        smoke_backend.create_event_subscriber(context, zmq, 18141)
    context.socket.return_value.close.assert_called_once_with(linger=0)


def test_ipc_transport_uses_private_path_and_connects_subscriber():
    path = PurePosixPath("/root/work/probe/kv.sock")
    command = smoke_backend.build_command(
        Path("/bin/vllm"), Path("/models/qwen"), 18140, events_ipc=path
    )
    config = json.loads(command[command.index("--kv-events-config") + 1])
    assert config["endpoint"] == "ipc:///root/work/probe/kv.sock"
    context, zmq = Mock(), Mock()
    subscriber = smoke_backend.create_event_subscriber(context, zmq, None, path)
    subscriber.connect.assert_called_once_with(config["endpoint"])
    subscriber.bind.assert_not_called()


@pytest.mark.parametrize(
    "path", [PurePosixPath("relative.sock"), PurePosixPath("/" + "x" * 101)]
)
def test_ipc_rejects_relative_or_too_long_paths(path):
    with pytest.raises(ValueError):
        smoke_backend.ipc_endpoint(path)


def test_command_and_subscriber_reject_ambiguous_transports():
    path = PurePosixPath("/root/probe.sock")
    with pytest.raises(ValueError):
        smoke_backend.build_command(
            Path("/bin/vllm"), Path("/model"), 1, 2, events_ipc=path
        )
    with pytest.raises(ValueError):
        smoke_backend.create_event_subscriber(Mock(), Mock(), 2, path)
    with pytest.raises(ValueError):
        smoke_backend.create_event_subscriber(Mock(), Mock(), None)


def test_ipc_connect_failure_closes_owned_socket():
    context, zmq = Mock(), Mock()
    context.socket.return_value.connect.side_effect = OSError("invalid endpoint")
    with pytest.raises(OSError):
        smoke_backend.create_event_subscriber(
            context, zmq, None, PurePosixPath("/root/probe.sock")
        )
    context.socket.return_value.close.assert_called_once_with(linger=0)


def test_hust_tagged_map_summary_excludes_private_fields():
    event = {
        "type": "BlockStored",
        "block_hashes": [12345],
        "parent_block_hash": None,
        "token_ids": [67890],
        "block_size": 128,
        "medium": "GPU",
        "group_idx": 0,
        "extra_keys": ["private"],
    }
    result = smoke_backend.summarize_event_batch([1.0, [event], 0])
    assert result[0]["block_count"] == result[0]["token_count"] == 1
    assert result[0]["block_size"] == 128
    assert "12345" not in json.dumps(result)
    assert "67890" not in json.dumps(result)
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "event",
    [{}, {"type": []}, {"type": "Unknown"}, {"type": "BlockStored"}, ["Unknown"]],
)
def test_unknown_or_incomplete_hust_event_fails_closed(event):
    with pytest.raises(ValueError):
        smoke_backend.summarize_event_batch([1.0, [event]])


def test_event_summary_rejects_non_scalar_medium():
    event = ["BlockStored", [1], None, [2], 128, None, {"private": 3}, None]
    with pytest.raises(ValueError):
        smoke_backend.summarize_event_batch([1.0, [event]])


def test_bytecode_cache_is_private_and_does_not_mutate_host_env(monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "host-value")
    path = Path("private-run")
    env = smoke_backend.build_environment(0, path)
    assert "PYTHONDONTWRITEBYTECODE" not in env
    assert env["PYTHONPYCACHEPREFIX"] == str(path / "cache" / "pyc")
    assert smoke_backend.os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert smoke_backend.os.environ["PYTHONPYCACHEPREFIX"] == "host-value"
    assert env["ASCEND_RT_VISIBLE_DEVICES"] == "0"


def test_event_summary_excludes_token_ids_and_hashes():
    result = smoke_backend.summarize_event_batch(
        [1.0, [["BlockStored", [12345], None, [67890], 16, None, "GPU", None]], None]
    )
    assert result == [
        {
            "kind": "BlockStored",
            "block_count": 1,
            "token_count": 1,
            "block_size": 16,
            "parent_present": False,
            "medium": "GPU",
        }
    ]
    assert "12345" not in json.dumps(result)
    assert "67890" not in json.dumps(result)


@pytest.mark.parametrize(
    "batch", [None, [], [0, None], [0, [[]]], [0, [["BlockStored"]]]]
)
def test_malformed_event_batch_fails(batch):
    with pytest.raises(ValueError):
        smoke_backend.summarize_event_batch(batch)


def test_cli_requires_explicit_execution():
    result = subprocess.run(
        [sys.executable, str(Path(smoke_backend.__file__))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "--execute" in result.stderr
