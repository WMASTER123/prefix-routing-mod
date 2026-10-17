"""A second CPU owner cannot replay/mutate a live owner's in-memory state."""

import subprocess
import sys
from queue import Queue
from threading import Thread

import pytest

from tests.test_component_contracts import worker
from tools.durable_ledger_prototype import DurableLedger


def test_second_owner_rejected_and_close_releases(tmp_path):
    path = tmp_path / "log"
    with (
        DurableLedger(path, create=True, workers=(worker(),)),
        pytest.raises(OSError),
    ):
        DurableLedger(path)
    with DurableLedger(path) as reopened:
        assert not reopened.admission_ready
    assert path.with_name("log.owner").is_file()


def test_failed_create_does_not_release_another_owner(tmp_path):
    path = tmp_path / "log"
    with DurableLedger(path, create=True, workers=(worker(),)):
        for _ in range(2):
            with pytest.raises(OSError):
                DurableLedger(path, create=True, workers=(worker(),))
        with pytest.raises(OSError):
            DurableLedger(path)


def test_bad_database_releases_lock(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"not sqlite")
    for _ in range(2):
        import sqlite3

        with pytest.raises(sqlite3.DatabaseError):
            DurableLedger(path)


@pytest.mark.parametrize("crash", [False, True])
def test_child_cannot_open_live_owner_then_can_open_after_exit(tmp_path, crash):
    path = tmp_path / "log"
    code = (
        "import os,sys; from pathlib import Path; "
        "from tools.durable_ledger_prototype import DurableLedger; "
        "from tests.test_component_contracts import worker; "
        "store=DurableLedger(Path(sys.argv[1]),create=True,workers=(worker(),)); "
        "print('ready',flush=True); sys.stdin.readline(); "
        + ("os._exit(37)" if crash else "store.close()")
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = Queue(maxsize=1)
        reader = Thread(
            target=lambda: ready.put(process.stdout.readline()), daemon=True
        )
        reader.start()
        assert ready.get(timeout=5).strip() == "ready"
        with pytest.raises(OSError):
            DurableLedger(path)
        stdout, stderr = process.communicate("stop\n", timeout=5)
        assert process.returncode == (37 if crash else 0), (stdout, stderr)
        with DurableLedger(path) as store:
            assert not store.admission_ready
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        reader.join(timeout=1)
