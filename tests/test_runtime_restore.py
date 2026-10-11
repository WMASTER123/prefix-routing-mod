import base64
import hashlib
import importlib.metadata as metadata
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import restore_runtime, verify_triton_overlay


def test_restore_manifest_locks_match_the_checked_in_candidate():
    manifest = restore_runtime.read_manifest()
    assert manifest["sources"]["ascend"]["commit"] == (
        "3a61053d8b4133eae7c468c1567648048750be21"
    )


def test_changed_lock_rejected_before_any_install(tmp_path, monkeypatch):
    (tmp_path / "deps.txt").write_text("changed")
    (tmp_path / "hust-restore.json").write_text(
        json.dumps({"locks": {"deps.txt": "0" * 64}})
    )
    monkeypatch.setattr(restore_runtime, "CANDIDATES", tmp_path)
    with pytest.raises(ValueError, match="lock changed"):
        restore_runtime.read_manifest()


def test_private_output_rejects_root_and_external_symlink(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "artifacts").mkdir()
    assert restore_runtime.private_path(root / "artifacts" / "fresh", root) == (
        root / "artifacts" / "fresh"
    )
    for path in (root, root / "artifacts", tmp_path / "elsewhere"):
        with pytest.raises(ValueError):
            restore_runtime.private_path(path, root)
    try:
        (root / "artifacts" / "redirect").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ValueError):
        restore_runtime.private_path(root / "artifacts" / "redirect" / "fresh", root)


def test_artifact_root_symlink_is_not_an_allowed_boundary(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "artifacts").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ValueError, match="redirected"):
        restore_runtime.private_path(root / "artifacts" / "fresh", root)


def test_restore_removes_package_sources_and_uses_only_private_python_cache(tmp_path):
    inherited = {
        "UV_INDEX_URL": "https://unrecorded.invalid",
        "UV_BUILD_CONSTRAINT": "unrecorded.txt",
        "PIP_EXTRA_INDEX_URL": "https://unrecorded.invalid",
        "GIT_CONFIG_COUNT": "1",
        "VIRTUAL_ENV": "/foreign",
        "PYTHONHOME": "/foreign",
        "PYTHONPATH": "/foreign",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LD_LIBRARY_PATH": "/necessary/cann/lib64",
    }
    env = restore_runtime.build_environment(tmp_path, "/cann/tbe", inherited)
    assert "UV_INDEX_URL" not in env and "PIP_EXTRA_INDEX_URL" not in env
    assert "UV_BUILD_CONSTRAINT" not in env and "GIT_CONFIG_COUNT" not in env
    assert "PYTHONHOME" not in env and "PYTHONDONTWRITEBYTECODE" not in env
    assert env["LD_LIBRARY_PATH"] == inherited["LD_LIBRARY_PATH"]
    assert env["PYTHONPATH"] == "/cann/tbe"
    assert Path(env["UV_CACHE_DIR"]).is_relative_to(tmp_path)
    assert Path(env["PYTHONPYCACHEPREFIX"]).is_relative_to(tmp_path)
    assert inherited["PYTHONPATH"] == "/foreign"


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("head", "wrong", "commit"),
        ("status", " M requirements.txt", "local changes"),
        ("shallow", "true", "Full source history"),
        ("origin", "https://other.invalid/source.git", "origin"),
    ],
)
def test_wrong_or_dirty_source_is_rejected(monkeypatch, field, value, match):
    values = {"head": "pinned", "status": "", "shallow": "false", "origin": "allowed"}
    values[field] = value
    keys = {
        ("rev-parse", "HEAD"): "head",
        ("status", "--porcelain", "--untracked-files=all"): "status",
        ("rev-parse", "--is-shallow-repository"): "shallow",
        ("remote", "get-url", "origin"): "origin",
    }
    monkeypatch.setattr(
        restore_runtime, "git_value", lambda path, *args: values[keys[args]]
    )
    with pytest.raises(ValueError, match=match):
        restore_runtime.verify_source(
            Path("/source"), {"commit": "pinned", "origin": "allowed"}
        )


def test_command_timeout_records_failure_and_stops_only_its_group(
    tmp_path, monkeypatch
):
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("uv", 1)
    monkeypatch.setattr(restore_runtime.subprocess, "Popen", lambda *a, **k: process)
    stop = Mock()
    monkeypatch.setattr(restore_runtime, "stop_command", stop)
    report = {"steps": []}
    with pytest.raises(TimeoutError):
        restore_runtime.run_step("install", ["uv"], tmp_path, {}, report, timeout=1)
    stop.assert_called_once_with(process)
    assert report["steps"][0]["status"] == "fail"
    assert report["steps"][0]["error"] == "timeout"


def test_failed_install_never_reports_a_passing_step(tmp_path, monkeypatch):
    process = Mock()
    process.wait.return_value = 1
    monkeypatch.setattr(restore_runtime.subprocess, "Popen", lambda *a, **k: process)
    report = {"steps": []}
    with pytest.raises(RuntimeError):
        restore_runtime.run_step("install", ["uv"], tmp_path, {}, report)
    assert report["steps"][0]["status"] == "fail"


def distribution(tmp_path, files, version="candidate"):
    return SimpleNamespace(
        files=files, version=version, locate_file=lambda p: tmp_path / p
    )


def entry(name, content):
    path = metadata.PackagePath(name)
    digest = (
        base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    )
    path.hash = metadata.FileHash("sha256=" + digest)
    return path


def test_overlay_detects_generic_triton_overwriting_ascend(tmp_path):
    (tmp_path / "triton").mkdir()
    path = tmp_path / "triton" / "shared.py"
    path.write_bytes(b"ascend")
    ascend = distribution(tmp_path, [entry("triton/shared.py", b"ascend")])
    generic = distribution(tmp_path, [entry("triton/shared.py", b"generic")])
    assert verify_triton_overlay.verify_files(generic, ascend)["status"] == "pass"
    path.write_bytes(b"generic")
    report = verify_triton_overlay.verify_files(generic, ascend)
    assert report["status"] == "fail"
    assert report["failures"] == [
        {"file": "triton/shared.py", "reason": "hash_mismatch"}
    ]
    path.unlink()
    assert verify_triton_overlay.verify_files(generic, ascend)["status"] == "fail"


def test_empty_record_or_no_overlap_cannot_pass(tmp_path):
    missing = distribution(tmp_path, [])
    assert verify_triton_overlay.verify_files(missing, missing)["status"] == "fail"
