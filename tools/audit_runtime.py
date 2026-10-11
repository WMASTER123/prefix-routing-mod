"""Read-only Python dependency audit, not a runtime or NPU compatibility test.

Run with the interpreter intended to serve requests. Never import the engines,
install packages, or modify the host. Reports belong in private artifacts.
"""

import argparse
import importlib.metadata as metadata
import json
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CORE_PACKAGES = ("vllm", "vllm-ascend", "torch", "torch-npu", "triton-ascend")


def redact_urls(text: str) -> str:
    """Retain source hosts/paths, never URL credentials, queries or fragments."""

    def redact(match):
        try:
            url = urlsplit(match[0])
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "<redacted-url>"

    return re.sub(r"(?:https?|file|git\+https)://[^\s\"'<>]+", redact, text)


def package_inventory(distributions) -> list[dict]:
    packages = []
    for dist in distributions:
        direct = dist.read_text("direct_url.json")
        source = json.loads(direct) if direct else {}
        packages.append(
            {
                "name": dist.metadata["Name"],
                "version": dist.version,
                "location": str(dist.locate_file("")),
                "source_url": redact_urls(source.get("url", "")) or None,
                "editable": bool(source.get("dir_info", {}).get("editable", False)),
                "vcs_commit": source.get("vcs_info", {}).get("commit_id"),
                "requirements": [redact_urls(r) for r in dist.requires or []],
            }
        )
    return sorted(packages, key=lambda p: (p["name"].lower(), p["version"]))


def check_dependencies() -> dict:
    command = [sys.executable, "-m", "pip", "check"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False
        )
        return {
            "command": command,
            "status": "pass" if result.returncode == 0 else "fail",
            "returncode": result.returncode,
            "stdout": redact_urls(result.stdout),
            "stderr": redact_urls(result.stderr),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": command, "status": "error", "error": type(error).__name__}


def make_report(packages: list[dict], dependency_check: dict) -> dict:
    names = {p["name"].lower().replace("_", "-") for p in packages}
    missing = sorted(set(CORE_PACKAGES) - names)
    return {
        "schema_version": 1,
        "scope": "python_dependency_audit_only",
        "collected_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "sys_path": sys.path,
        "packages": packages,
        "missing_core_packages": missing,
        "dependency_check": dependency_check,
        "status": (
            "pass" if not missing and dependency_check["status"] == "pass" else "fail"
        ),
        "artifact_lock_verified": False,
        "runtime_compatibility_verified": False,
        "environment_gate_passed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output already exists; preserve previous evidence")
    packages = package_inventory(metadata.distributions())
    report = make_report(packages, check_dependencies())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(f"Dependency audit: {report['status']}; saved {args.output}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
