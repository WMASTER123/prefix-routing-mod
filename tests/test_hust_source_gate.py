"""Source gate guards and bounded download failures, without network or HUST."""

import ast
import hashlib
import io
import json
from contextlib import closing

import pytest

from prefix_routing_mod.adapters.profiles import get_profile
from prefix_routing_mod.adapters.terminal_guard import terminal_profile
from tools import audit_backend_terminal, audit_http_entry
from tools import fetch_hust_audit_sources as fetch
from tools import hust_source_profiles as profiles


def test_source_inputs_agree_with_runtime_profile():
    assert get_profile(profiles.CED_PROFILE).core_commit == profiles.CED_COMMIT
    hashes = profiles.CED_CAPTURE_HASHES | profiles.CED_ENTRY_HASHES
    assert set(hashes) == set(profiles.CED_SOURCE_PATHS)
    assert all(len(value) == 64 for value in hashes.values())
    assert all(path.startswith("vllm/") for path in profiles.CED_SOURCE_PATHS.values())
    packaged = terminal_profile()["sources"]
    assert all(
        packaged[profiles.CED_SOURCE_PATHS[name]] == value
        for name, value in hashes.items()
    )


@pytest.mark.parametrize("audit", [audit_backend_terminal, audit_http_entry])
def test_unknown_profile_rejected_before_source_execution(tmp_path, audit):
    args = (tmp_path, tmp_path) if audit is audit_http_entry else (tmp_path,)
    with pytest.raises(ValueError, match="Unknown"):
        audit.run_audit(*args, profile="future-unverified")


def test_duplicate_source_method_rejected():
    tree = ast.parse("class A:\n def selected(self): pass\n def selected(self): pass")
    with pytest.raises(ValueError, match="method set"):
        audit_backend_terminal.source_class({"f": tree}, "f", "A", ("selected",), {})


def fake_inputs(monkeypatch):
    payloads = {name: name.encode() for name in profiles.CED_SOURCE_PATHS}
    hashes = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()
    }
    monkeypatch.setattr(fetch, "CED_CAPTURE_HASHES", hashes)
    monkeypatch.setattr(fetch, "CED_ENTRY_HASHES", {})
    reverse = {path: name for name, path in profiles.CED_SOURCE_PATHS.items()}

    def open_url(url, *, timeout):
        prefix = (
            "https://raw.githubusercontent.com/vLLM-HUST/vllm-hust/"
            f"{profiles.CED_COMMIT}/"
        )
        assert url.startswith(prefix) and timeout == 20
        return closing(io.BytesIO(payloads[reverse[url.removeprefix(prefix)]]))

    return payloads, open_url


def test_fetch_fixed_inputs_and_preserve_existing_evidence(tmp_path, monkeypatch):
    payloads, open_url = fake_inputs(monkeypatch)
    output = tmp_path / "sources"
    assert fetch.fetch_sources(output, open_url=open_url)["status"] == "pass"
    assert all((output / name).read_bytes() == data for name, data in payloads.items())
    with pytest.raises(FileExistsError):
        fetch.fetch_sources(output, open_url=open_url)
    assert json.loads((output / "fetch.json").read_text())["status"] == "pass"


def test_fetch_transient_network_retry_is_bounded_and_recorded(tmp_path, monkeypatch):
    _, original = fake_inputs(monkeypatch)
    attempts = 0

    def open_url(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic transient TLS error")
        return original(*args, **kwargs)

    report = fetch.fetch_sources(tmp_path / "sources", open_url=open_url)
    assert report["status"] == "pass"
    assert report["attempts"][next(iter(profiles.CED_SOURCE_PATHS))] == 2


@pytest.mark.parametrize("failure", ["hash", "budget", "network"])
def test_fetch_rejects_bad_sources_and_records_failure(tmp_path, monkeypatch, failure):
    payloads, original = fake_inputs(monkeypatch)
    first = next(iter(payloads))
    if failure == "hash":
        payloads[first] = b"changed"
    elif failure == "budget":
        monkeypatch.setattr(fetch, "MAX_SOURCE_BYTES", 1)

    def open_url(*args, **kwargs):
        if failure == "network":
            raise TimeoutError("synthetic network timeout")
        return original(*args, **kwargs)

    output = tmp_path / "sources"
    with pytest.raises((ValueError, TimeoutError)):
        fetch.fetch_sources(output, open_url=open_url)
    assert not (output / first).exists()
    assert json.loads((output / "fetch.json").read_text())["status"] == "fail"
    attempts = json.loads((output / "fetch.json").read_text())["attempts"]
    assert attempts[first] == (3 if failure == "network" else 1)
