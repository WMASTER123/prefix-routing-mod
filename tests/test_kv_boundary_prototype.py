import queue
import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.prototype_kv_boundary import PublisherBoundaryPrototype


def prototype(encode):
    publisher = SimpleNamespace(
        _event_queue=queue.Queue(maxsize=2),
        _next_seq=0,
        _buffer=deque(maxlen=2),
        _apply_to_mirror=Mock(),
        _build_replay_response=Mock(),
    )
    return PublisherBoundaryPrototype(publisher, encode), publisher


def test_send_failure_retains_committed_replay_and_balances_queue():
    boundary, publisher = prototype(lambda _: b"payload")
    boundary.enqueue(SimpleNamespace(events=["stored"]))
    with pytest.raises(OSError):
        boundary.commit_next(Mock(side_effect=OSError("PUB send failed")))
    assert publisher._next_seq == 1
    assert list(publisher._buffer) == [(0, b"payload")]
    assert publisher._event_queue.unfinished_tasks == 0
    assert not boundary.failed
    boundary.recover("request")
    publisher._build_replay_response.assert_called_once_with("request")


def test_encoding_failure_keeps_watermark_and_disables_snapshot():
    boundary, publisher = prototype(Mock(side_effect=ValueError("encoding")))
    boundary.enqueue(SimpleNamespace(events=["stored"]))
    with pytest.raises(ValueError, match="encoding"):
        boundary.commit_next(Mock())
    assert publisher._next_seq == 0 and not publisher._buffer
    publisher._apply_to_mirror.assert_not_called()
    assert publisher._event_queue.unfinished_tasks == 0
    with pytest.raises(RuntimeError, match="unavailable"):
        boundary.recover("request")


def test_snapshot_and_commit_reject_another_owner_thread():
    boundary, publisher = prototype(lambda _: b"payload")
    errors = []

    def other_thread():
        try:
            boundary.recover("request")
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == ["Snapshot and commit need the same owner thread"]
    publisher._build_replay_response.assert_not_called()


def test_full_queue_rejects_enqueue_without_mirror_mutation():
    boundary, publisher = prototype(lambda _: b"payload")
    boundary.enqueue("one")
    boundary.enqueue("two")
    with pytest.raises(queue.Full):
        boundary.enqueue("three")
    assert publisher._event_queue.qsize() == 2
    publisher._apply_to_mirror.assert_not_called()
