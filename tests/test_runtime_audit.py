import json
import subprocess
from unittest.mock import Mock

import pytest

from tools import audit_runtime


def core_packages():
    return [{"name": n, "version": "test"} for n in audit_runtime.CORE_PACKAGES]


def test_empty_venv_cannot_pass_service_audit():
    report = audit_runtime.make_report([], {"status": "pass"})
    assert report["status"] == "fail"
    assert len(report["missing_core_packages"]) == 5


def test_dependency_pass_is_not_runtime_compatibility():
    report = audit_runtime.make_report(core_packages(), {"status": "pass"})
    assert report["status"] == "pass"
    assert report["runtime_compatibility_verified"] is False
    assert report["artifact_lock_verified"] is False
    assert report["environment_gate_passed"] is False


def test_hust_distribution_must_be_selected_explicitly():
    packages = [p for p in core_packages() if p["name"] != "vllm-ascend"]
    packages.append({"name": "vllm_ascend_hust", "version": "test"})
    assert audit_runtime.make_report(packages, {"status": "pass"})["status"] == "fail"
    report = audit_runtime.make_report(packages, {"status": "pass"}, "vllm-ascend-hust")
    assert report["status"] == "pass"
    assert report["selected_ascend_distribution"] == "vllm-ascend-hust"
    assert report["runtime_compatibility_verified"] is False


def test_upstream_package_cannot_satisfy_hust_selection():
    report = audit_runtime.make_report(
        core_packages(), {"status": "pass"}, "vllm-ascend-hust"
    )
    assert report["status"] == "fail"
    assert report["missing_core_packages"] == ["vllm-ascend-hust"]


def test_two_distributions_sharing_ascend_module_fail_audit():
    packages = [*core_packages(), {"name": "vllm-ascend-hust", "version": "test"}]
    report = audit_runtime.make_report(packages, {"status": "pass"}, "vllm-ascend-hust")
    assert report["status"] == "fail"
    assert report["missing_core_packages"] == []
    assert report["conflicting_ascend_distributions"] == [
        "vllm-ascend",
        "vllm-ascend-hust",
    ]


def test_invalid_distribution_selector_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        audit_runtime.make_report(core_packages(), {"status": "pass"}, "anything")


def test_distribution_names_follow_package_name_normalization():
    assert audit_runtime.normalize_name("VLLM..Ascend__Hust") == "vllm-ascend-hust"


@pytest.mark.parametrize("status", ["fail", "error"])
def test_dependency_failure_fails_audit(status):
    assert (
        audit_runtime.make_report(core_packages(), {"status": status})["status"]
        == "fail"
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://user:secret@example.org/a?token=hidden#x", "https://example.org/a"),
        ("git+https://secret@example.org/repo", "git+https://example.org/repo"),
        ("file:///local/repo", "file:///local/repo"),
    ],
)
def test_source_url_redaction(url, expected):
    assert audit_runtime.redact_urls(url) == expected


def test_inventory_reads_metadata_without_importing_backend():
    dist = Mock(version="0.23.0", requires=["numpy>=2"])
    dist.metadata = {"Name": "vllm"}
    dist.read_text.return_value = json.dumps(
        {"url": "file:///source/vllm", "dir_info": {"editable": True}}
    )
    dist.locate_file.return_value = "/env/site-packages"
    package = audit_runtime.package_inventory([dist])[0]
    assert package["editable"] is True
    assert package["source_url"] == "file:///source/vllm"
    assert package["requirements"] == ["numpy>=2"]


def test_pip_failure_and_timeout_are_not_success(monkeypatch):
    run = Mock(return_value=Mock(returncode=1, stdout="conflict", stderr=""))
    monkeypatch.setattr(audit_runtime.subprocess, "run", run)
    assert audit_runtime.check_dependencies()["status"] == "fail"
    assert run.call_args.kwargs["timeout"] == 60
    assert run.call_args.args[0][0] == audit_runtime.sys.executable
    run.side_effect = subprocess.TimeoutExpired("pip", 60)
    assert audit_runtime.check_dependencies()["status"] == "error"


def test_cli_preserves_existing_evidence(tmp_path):
    output = tmp_path / "audit.json"
    output.write_text("previous", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        audit_runtime.main(["--output", str(output)])
    assert error.value.code == 2
    assert output.read_text("utf-8") == "previous"


def test_cli_writes_failure_report_and_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_runtime.metadata, "distributions", lambda: [])
    monkeypatch.setattr(audit_runtime, "check_dependencies", lambda: {"status": "pass"})
    output = tmp_path / "audit.json"
    assert audit_runtime.main(["--output", str(output)]) == 1
    assert json.loads(output.read_text("utf-8"))["status"] == "fail"
