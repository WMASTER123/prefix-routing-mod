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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    # Imports happen only after explicit execution, never on MOD discovery.
    import torch
    import torch_npu
    import triton
    import vllm
    import vllm_ascend
    from triton.backends import backends

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
        "distributions": {
            name: metadata.version(name)
            for name in ("vllm", "vllm-ascend-hust", "torch", "torch-npu")
        },
        "triton_backends": sorted(backends),
        "python_path": os.environ.get("PYTHONPATH"),
        "ld_library_path": os.environ.get("LD_LIBRARY_PATH"),
        "loaded_libraries": loaded_libraries(),
        "component_version_files": component_versions(),
        "environment_gate_passed": False,
    }
    report["status"] = "pass" if set(backends) == {"ascend"} else "fail"
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
