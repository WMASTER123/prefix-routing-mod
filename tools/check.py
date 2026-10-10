"""Unified manual/CI entry point; unavailable suites never report pass."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "cpu": [
        ["-m", "ruff", "check", "."],
        ["-m", "ruff", "format", "--check", "."],
        [
            "-m",
            "pytest",
            "-q",
            "-m",
            "not integration",
            "--junitxml=artifacts/cpu-junit.xml",
        ],
    ],
    "integration": [
        [
            "-m",
            "pytest",
            "-q",
            "-m",
            "integration",
            "--junitxml=artifacts/integration-junit.xml",
        ],
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        required=True,
        choices=[*SUITES, "npu-smoke", "npu-routing", "performance", "release"],
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "checks")
    args = parser.parse_args()
    report = {
        "schema_version": 1,
        "suite": args.suite,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "not-run",
        "commands": [],
    }
    code = 3
    if args.suite in SUITES:
        code = 0
        report["status"] = "pass"
        for command in SUITES[args.suite]:
            result = subprocess.run([sys.executable, *command], cwd=ROOT, check=False)
            report["commands"].append(
                {"argv": command, "returncode": result.returncode}
            )
            if result.returncode:
                code = 1
                report["status"] = "fail"
                break
    else:
        report["reason"] = "Suite not implemented; hardware/release gate remains open"
    report["finished_at"] = datetime.now(UTC).isoformat()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"{args.suite}.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
