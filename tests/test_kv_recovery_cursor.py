import pytest

from tools.kv_recovery_cursor import RecoveryCursor


def frames(seq, payload=b"batch"):
    return [b"", seq.to_bytes(8, "big"), payload]


def replay(epoch="a", start=0, end=2, **changes):
    return {
        "publisher_epoch": epoch,
        "next_seq": end,
        "replayed_batches": [[i, b"batch"] for i in range(start, end)],
        **changes,
    }


def ready():
    cursor = RecoveryCursor(recent_limit=4)
    cursor.start_supervised_empty("a", 0, now=0)
    return cursor


def test_gap_is_not_applied_and_complete_replay_restores_continuity():
    cursor = ready()
    assert cursor.accept_live(frames(0), generation=0) == "accepted"
    assert cursor.accept_live(frames(3), generation=0) == "gap"
    assert cursor.next_seq == 1 and not cursor.continuous
    assert cursor.accept_live(frames(1), generation=0) == "unknown"
    assert cursor.recover(replay(start=1, end=4), now=1) == "recovered"
    assert cursor.next_seq == 4 and cursor.continuous
    assert cursor.accept_live(frames(3), generation=0) == "duplicate"
    assert cursor.next_seq == 4


def test_partial_replay_is_rejected_without_partial_application():
    cursor = ready()
    with pytest.raises(ValueError):
        cursor.recover(replay(replayed_batches=[[0, b"batch"]]), now=1)
    assert cursor.next_seq == 0 and not cursor.recent and not cursor.continuous


def test_snapshot_never_grants_continuity_even_when_watermark_looks_valid():
    cursor = ready()
    assert (
        cursor.recover(replay(end=2, replayed_batches=[], snapshot=b"snapshot"), now=1)
        == "snapshot_unknown"
    )
    assert not cursor.continuous and not cursor.has_origin
    assert cursor.recover(replay(), now=2) == "unknown"
    assert cursor.next_seq == 0


def test_restart_forgets_origin_and_late_old_connection_is_rejected():
    cursor = ready()
    cursor.accept_live(frames(0), generation=0)
    assert (
        cursor.recover(replay(epoch="b", end=0, snapshot=b"empty"), now=1)
        == "new_epoch"
    )
    cursor.replace_connection()
    assert cursor.accept_live(frames(0), generation=0) == "old_connection"
    assert cursor.next_seq == 0 and not cursor.has_origin
    cursor.start_supervised_empty("b", 0, now=2)
    assert cursor.accept_live(frames(0), generation=1) == "accepted"


def test_idle_is_valid_with_control_confirmation_but_live_does_not_renew_lease():
    cursor = ready()
    cursor.recover(replay(end=0), now=4)
    assert not cursor.expire(now=8, max_age=5)
    cursor.accept_live(frames(0), generation=0)
    assert cursor.expire(now=9, max_age=5)
    assert not cursor.continuous and cursor.next_seq == 1
    cursor.recover(replay(start=1, end=1), now=10)
    assert cursor.continuous


def test_conflicting_duplicate_rejected_and_memory_bounded():
    cursor = ready()
    for seq in range(5):
        cursor.accept_live(frames(seq), generation=0)
    assert list(cursor.recent) == [1, 2, 3, 4]
    with pytest.raises(ValueError, match="Duplicate"):
        cursor.accept_live(frames(4, b"different"), generation=0)
    assert cursor.next_seq == 5 and not cursor.continuous


@pytest.mark.parametrize(
    "payload", [b"", b"x" * (1024**2 + 1)], ids=["empty", "oversized"]
)
def test_invalid_replay_payload_preserves_cursor(payload):
    cursor = ready()
    with pytest.raises(ValueError):
        cursor.recover(replay(end=1, replayed_batches=[[0, payload]]), now=1)
    assert cursor.next_seq == 0 and not cursor.continuous


@pytest.mark.parametrize("now", [True, -1, float("inf"), float("nan")])
def test_invalid_time_cannot_grant_continuity(now):
    cursor = RecoveryCursor()
    with pytest.raises(ValueError):
        cursor.start_supervised_empty("a", 0, now=now)
    assert not cursor.continuous


def test_unannounced_sequence_reset_invalidates_existing_stream():
    cursor = ready()
    cursor.accept_live(frames(0), generation=0)
    with pytest.raises(ValueError):
        cursor.accept_live(frames(0, b"new-process"), generation=0)
    assert not cursor.continuous
