"""Optional fixed-source CPU execution; mandatory in the dedicated CI job.

HUST_AUDIT_SOURCE_DIR selects explicitly downloaded, hash-checked source files.
This does not import HUST or certify a live engine/IPC/device configuration.
"""

import os
from pathlib import Path

import pytest

from tools import audit_backend_terminal as terminal
from tools import audit_http_entry as entry
from tools.hust_source_profiles import CED_PROFILE


@pytest.fixture
def sources():
    configured = os.environ.get("HUST_AUDIT_SOURCE_DIR")
    if not configured:
        pytest.skip("fixed-source execution runs in the explicit HUST CI gate")
    directory = Path(configured)
    if not directory.is_dir():
        pytest.fail("explicit HUST source directory is missing")
    return directory


def test_current_terminal_and_entry_ledger_contracts(sources):
    report = terminal.run_audit(sources, profile=CED_PROFILE)
    assert report["status"] == "pass" and all(report["checks"].values())
    report = entry.run_audit(sources, sources, profile=CED_PROFILE)
    assert report["status"] == "pass" and all(report["checks"].values())
    assert report["checks"]["cancel_commit_before_ack_retains_replay"]
    assert report["outcome_health_clock_and_ipc_are_synthetic"]
    assert not report["network_http_validated"] and not report["observer_installed"]


def test_unexpected_cleanup_error_is_not_an_expected_fault(sources, monkeypatch):
    original = entry.scheduler_fixture

    def broken(*args, **kwargs):
        value = original(*args, **kwargs)

        def fail(_):
            raise LookupError("unexpected fixture failure must propagate")

        value.kv_cache_manager.free = fail
        return value

    monkeypatch.setattr(entry, "scheduler_fixture", broken)
    with pytest.raises(Exception) as raised:
        entry.run_audit(sources, sources, profile=CED_PROFILE)
    cause = raised.value
    while cause.__cause__ is not None:
        cause = cause.__cause__
    assert isinstance(cause, LookupError)


def test_missing_family_seal_fails_the_gate(sources, monkeypatch):
    monkeypatch.setattr(entry.EntryCapture, "submitted", lambda *args: None)
    report = entry.run_audit(sources, sources, profile=CED_PROFILE)
    assert report["status"] == "fail"
    assert not report["source_methods_to_existing_ledger_verified"]
