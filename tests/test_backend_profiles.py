import hashlib
import importlib.metadata as metadata
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from prefix_routing_mod import cli
from prefix_routing_mod.adapters import profiles

NEW = "hust-cedb477-native-v1"


def fake_install(tmp_path, monkeypatch):
    profile = profiles.get_profile(NEW)
    sources = []
    for relative, _ in profile.source_hashes:
        file = tmp_path / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"audited fixture")
        sources.append((relative, hashlib.sha256(file.read_bytes()).hexdigest()))
    profile = replace(profile, source_hashes=tuple(sources))
    monkeypatch.setattr(profiles, "get_profile", lambda _: profile)
    versions = dict(profile.packages)

    def distribution(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return NS(version=versions[name], locate_file=lambda path: tmp_path / path)

    return profile, versions, distribution


def test_profiles_separate_protocol_and_recovery_capabilities():
    old, new = (
        profiles.get_profile("hust-legacy-publisher-v1"),
        profiles.get_profile(NEW),
    )
    assert old.publisher_hook_available and old.native_epoch
    assert not new.native_epoch and not new.native_token_snapshot
    assert not new.publisher_hook_available and not new.terminal_observer_available
    assert new.native_replay == "sequence-end-v1"
    assert dict(new.packages)["vllm-ascend"].endswith("gb47795c63")


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="Unknown"):
        profiles.get_profile("unqualified")


def test_interface_check_cannot_promote_runtime_or_recovery(tmp_path, monkeypatch):
    _, _, distribution = fake_install(tmp_path, monkeypatch)
    report = profiles.inspect_backend(NEW, distribution=distribution)
    assert report["interface_verified"]
    assert not report["runtime_verified"] and not report["routing_ready"]
    assert not report["full_recovery_verified"]


@pytest.mark.parametrize("failure", ["missing", "version", "mixed", "bytes", "escape"])
def test_reject_unqualified_installation(tmp_path, monkeypatch, failure):
    profile, versions, distribution = fake_install(tmp_path, monkeypatch)
    if failure == "missing":
        del versions["vllm"]
    elif failure == "version":
        versions["vllm"] = "0.31"
    elif failure == "mixed":
        versions["vllm-ascend-hust"] = "another"
    elif failure == "bytes":
        (tmp_path / profile.source_hashes[0][0]).write_bytes(b"changed")
    else:
        original = distribution

        def distribution(name):
            dist = original(name)
            dist.locate_file = lambda path: tmp_path if "/" not in path else Path("/")
            return dist

    with pytest.raises((OSError, RuntimeError)):
        profiles.inspect_backend(NEW, distribution=distribution)


def test_backend_doctor_missing_installation_is_structured_error(monkeypatch, capsys):
    def missing(_):
        raise RuntimeError("Backend package is missing")

    monkeypatch.setattr(cli, "inspect_backend", missing)
    assert cli.main(["backend-doctor", "--profile", NEW]) == 2
    assert '"interface_verified": false' in capsys.readouterr().out
