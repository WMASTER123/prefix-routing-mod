"""Read-only inventory. Does not import engines or modify shared resources."""

import argparse
import importlib.metadata
import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    versions = {}
    for package in ("torch", "torch-npu", "vllm", "vllm-ascend"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    commands = {}
    for command in (["npu-smi", "info"], ["findmnt", "-T", "/root"]):
        if not shutil.which(command[0]):
            commands[command[0]] = {"status": "unavailable"}
            continue
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            commands[command[0]] = {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            commands[command[0]] = {"status": "error", "detail": str(error)}
    report = {
        "collected_at": datetime.now(UTC).isoformat(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "packages_in_this_interpreter": versions,
        "commands": commands,
        "runtime_compatibility_verified": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Inventory saved: {args.output}; runtime compatibility is unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
