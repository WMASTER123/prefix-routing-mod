"""Explicit runtime imports and loaded-library evidence; not a device benchmark."""

import argparse
import importlib.metadata as metadata
import json
import os
import platform
import subprocess
from pathlib import Path

VERSION_FILES = (
    "/usr/local/Ascend/driver/version.info",
    "/usr/local/Ascend/cann-9.1.0/compiler/version.info",
    "/usr/local/Ascend/cann-9.1.0/opp/version.info",
    "/usr/local/Ascend/cann-9.1.0/aarch64-linux/ascend_toolkit_install.info",
)


def component_versions() -> dict:
    return {
        name: Path(name).read_text() if Path(name).is_file() else None
        for name in VERSION_FILES
    }


def loaded_libraries() -> list[str]:
    paths = set()
    for line in Path("/proc/self/maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5].startswith("/") and ".so" in fields[5]:
            paths.add(fields[5])
    return sorted(paths)


def distribution_versions(profile_id=None, *, version=None) -> dict[str, str]:
    """Use the qualified mapping, never guess an alias from installed packages."""
    version = metadata.version if version is None else version
    engines = ("vllm", "vllm-ascend-hust")
    if profile_id is not None:
        from prefix_routing_mod.adapters.profiles import get_profile

        engines = tuple(name for name, _ in get_profile(profile_id).packages)
    return {name: version(name) for name in (*engines, "torch", "torch-npu")}


def active_backends(backends) -> list[str]:
    """Mirror the installed Triton driver's unique-active selection check."""
    return sorted(
        name for name, backend in backends.items() if backend.driver.is_active()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--profile",
        help="Verify fixed backend interface and use its distribution mapping",
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    # Reject an unqualified profile before importing engines or touching devices.
    interface = None
    if args.profile is not None:
        from prefix_routing_mod.adapters.profiles import inspect_backend

        interface = inspect_backend(args.profile)
    # Imports happen only after explicit execution, never on MOD discovery.
    import torch
    import torch_npu
    import triton
    import vllm
    import vllm_ascend
    from triton.backends import backends

    active = active_backends(backends)
    report = {
        "schema_version": 1,
        "scope": "explicit_import_and_library_inventory",
        "python": platform.python_version(),
        "machine": platform.machine(),
        "import_versions": {
            "torch": torch.__version__,
            "torch_npu": torch_npu.__version__,
            "triton": triton.__version__,
            "vllm": vllm.__version__,
        },
        "modules": {
            name: str(Path(module.__file__).resolve())
            for name, module in {"vllm": vllm, "vllm_ascend": vllm_ascend}.items()
        },
        "distributions": distribution_versions(args.profile),
        "backend_interface": interface,
        "triton_backends": sorted(backends),
        "triton_active_backends": active,
        "python_path": os.environ.get("PYTHONPATH"),
        "ld_library_path": os.environ.get("LD_LIBRARY_PATH"),
        "loaded_libraries": loaded_libraries(),
        "component_version_files": component_versions(),
        "environment_gate_passed": False,
    }
    report["status"] = "pass" if active == ["ascend"] else "fail"
    if platform.system() == "Linux":
        result = subprocess.run(
            ["npu-smi", "info", "-m"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        report["device_mapping"] = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.returncode:
            report["status"] = "fail"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(f"Runtime imports: {report['status']}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
