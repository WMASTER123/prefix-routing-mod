import builtins
import importlib.metadata as metadata
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import inspect_runtime


def test_current_profile_uses_its_exact_distribution_names(monkeypatch):
    monkeypatch.setattr(
        "prefix_routing_mod.adapters.profiles.get_profile",
        lambda name: (
            SimpleNamespace(packages=(("vllm", "core"), ("vllm-ascend", "ascend")))
            if name == "fixed"
            else pytest.fail("Unexpected profile")
        ),
    )
    installed = {
        "vllm": "core",
        "vllm-ascend": "ascend",
        "torch": "torch",
        "torch-npu": "npu",
    }
    assert (
        inspect_runtime.distribution_versions("fixed", version=installed.__getitem__)
        == installed
    )


def test_missing_mapped_distribution_is_not_replaced_with_another_alias(monkeypatch):
    monkeypatch.setattr(
        "prefix_routing_mod.adapters.profiles.get_profile",
        lambda _: SimpleNamespace(
            packages=(("vllm", "core"), ("vllm-ascend", "ascend"))
        ),
    )
    calls = []

    def version(name):
        calls.append(name)
        if name == "vllm-ascend":
            raise metadata.PackageNotFoundError(name)
        return "installed"

    with pytest.raises(metadata.PackageNotFoundError):
        inspect_runtime.distribution_versions("fixed", version=version)
    assert calls == ["vllm", "vllm-ascend"]


def test_legacy_inventory_remains_explicitly_legacy():
    calls = []
    inspect_runtime.distribution_versions(
        version=lambda name: calls.append(name) or "old"
    )
    assert calls == ["vllm", "vllm-ascend-hust", "torch", "torch-npu"]


@pytest.mark.parametrize(
    "active", [{"ascend"}, set(), {"nvidia"}, {"ascend", "nvidia"}]
)
def test_registered_backends_are_not_confused_with_active_drivers(active):
    backends = {
        name: SimpleNamespace(
            driver=SimpleNamespace(is_active=lambda name=name: name in active)
        )
        for name in ("amd", "ascend", "nvidia")
    }
    assert inspect_runtime.active_backends(backends) == sorted(active)


def test_invalid_interface_fails_before_engine_import_or_output(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        ["inspect_runtime", "--execute", "--profile", "wrong", "--output", str(output)],
    )

    def reject(_):
        raise RuntimeError("unqualified interface")

    monkeypatch.setattr("prefix_routing_mod.adapters.profiles.inspect_backend", reject)
    with pytest.raises(RuntimeError, match="unqualified"):
        inspect_runtime.main()
    assert not output.exists()


def test_standalone_help_does_not_require_installed_mod_or_engine(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith(("prefix_routing_mod", "torch", "triton", "vllm")):
            pytest.fail("Standalone help must not import MOD or engines")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr("sys.argv", ["inspect_runtime", "--help"])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(inspect_runtime.__file__)), run_name="__main__")
    assert result.value.code == 0
