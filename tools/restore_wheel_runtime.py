"""Restore qualified owner-local wheels into a fresh project-private environment.

Requires the complete, hash-checked wheelhouse and locked dependency assets.
Does not rebuild engines, select new versions or certify model/runtime behavior.
"""

import argparse
import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from tools.restore_runtime import ROOT, build_environment, private_path, run_step

CANDIDATES = ROOT / "runtime/candidates"


def load_inputs(wheelhouse):
    manifest = json.loads((CANDIDATES / "hust-cedb477.json").read_text("utf-8"))
    for directory, entries in (
        (CANDIDATES, manifest["locks"]),
        (wheelhouse, manifest["wheels"]),
    ):
        for name, expected in entries.items():
            path = directory / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Candidate input fingerprint differs: {name}")
    return manifest


def installation_steps(uv, python, output, wheelhouse, manifest, *, offline=False):
    binary = output / "service-venv/bin/python"
    install = [str(uv), "--no-config", "pip", "install", "--python", str(binary)]
    if offline:
        install.append("--offline")
    common = [
        "--find-links",
        str(wheelhouse),
        "--default-index",
        manifest["indexes"][0],
    ]
    for index in manifest["indexes"][1:]:
        common += ["--index", index]
    common += ["--index-strategy", "unsafe-best-match"]
    wheels = manifest["wheels"]
    return [
        (
            "venv",
            [
                str(uv),
                "--no-config",
                "venv",
                "--python",
                str(python),
                str(binary.parent.parent),
            ],
        ),
        (
            "dependencies",
            install
            + common
            + [
                "--require-hashes",
                "--only-binary",
                ":all:",
                "-r",
                str(CANDIDATES / "hust-cedb477-py312-aarch64.txt"),
            ],
        ),
        (
            "extra",
            install
            + common
            + [
                "--require-hashes",
                "--no-deps",
                "-r",
                str(CANDIDATES / "hust-cedb477-extra.txt"),
            ],
        ),
        (
            "triton-overlay",
            install
            + [
                "--no-deps",
                "--reinstall",
                str(
                    wheelhouse
                    / next(name for name in wheels if name.startswith("triton_ascend-"))
                ),
            ],
        ),
        (
            "engines",
            install
            + [
                "--no-deps",
                *[
                    str(wheelhouse / name)
                    for name in wheels
                    if name.startswith(("vllm-", "vllm_ascend-"))
                ],
            ],
        ),
        (
            "dependencies-check",
            [str(uv), "--no-config", "pip", "check", "--python", str(binary)],
        ),
        (
            "overlay-check",
            [
                str(binary),
                str(ROOT / "tools/verify_triton_overlay.py"),
                "--output",
                str(output / "overlay.json"),
            ],
        ),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    for name in ("wheelhouse", "uv", "python", "output", "cache-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        wheelhouse = private_path(args.wheelhouse).resolve(strict=True)
        output, cache = private_path(args.output), private_path(args.cache_dir)
        if output.exists():
            raise ValueError("Output exists; preserve prior evidence")
        manifest = load_inputs(wheelhouse)
        if platform.machine() != manifest["machine"]:
            raise ValueError("Candidate requires aarch64")
        for executable, expected, flag in (
            (args.python, "Python " + manifest["python"], "--version"),
            (args.uv, "uv " + manifest["uv"], "--version"),
        ):
            actual = subprocess.run(
                [str(executable), flag],
                check=True,
                text=True,
                capture_output=True,
                timeout=10,
            ).stdout.strip()
            if not actual.startswith(expected + " ") and actual != expected:
                raise ValueError("Candidate interpreter/installer version differs")
        output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    report = {
        "status": "fail",
        "scope": manifest["scope"],
        "profile_id": manifest["profile_id"],
        "started_at": datetime.now(UTC).isoformat(),
        "steps": [],
        "locks": manifest["locks"],
        "wheels": manifest["wheels"],
        "runtime_verified": False,
        "model_verified": False,
    }
    env = build_environment(output, manifest["cann_acl_path"])
    env["UV_CACHE_DIR"] = str(cache)
    env.pop("PYTHONPATH", None)
    try:
        for name, command in installation_steps(
            args.uv, args.python, output, wheelhouse, manifest, offline=args.offline
        ):
            run_step(name, command, output, env, report)
        report["status"] = "pass"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["finished_at"] = datetime.now(UTC).isoformat()
    (output / "restore.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(json.dumps({"status": report["status"], "output": str(output)}))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
