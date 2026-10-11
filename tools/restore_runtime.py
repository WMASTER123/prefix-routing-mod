"""Restore the fixed HUST candidate into a fresh project-private directory.

This is a same-host development recipe, not a production image installer.
Explicit execution builds only the recorded sources and runs a bounded smoke.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "runtime" / "candidates"


def private_path(path: Path, root: Path = ROOT) -> Path:
    resolved = path.resolve()
    boundary = (root / "artifacts").resolve()
    if boundary != root.resolve() / "artifacts":
        raise ValueError("Project artifacts cannot be redirected outside the project")
    if resolved == boundary or boundary not in resolved.parents:
        raise ValueError("Path must be below this project's artifacts directory")
    return resolved


def read_manifest() -> dict:
    manifest = json.loads((CANDIDATES / "hust-restore.json").read_text("utf-8"))
    for name, digest in manifest["locks"].items():
        data = (CANDIDATES / name).read_text("utf-8").encode()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Candidate lock changed: {name}")
    return manifest


def build_environment(output: Path, cann_path: str, inherited=None) -> dict:
    env = dict(os.environ if inherited is None else inherited)
    for name in list(env):
        if name.startswith(("UV_", "PIP_", "GIT_CONFIG_")) or name in {
            "VIRTUAL_ENV",
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONDONTWRITEBYTECODE",
        }:
            env.pop(name)
    env.update(
        {
            "PYTHONPATH": cann_path,
            "PYTHONPYCACHEPREFIX": str(output / "cache" / "pyc"),
            "UV_CACHE_DIR": str(output / "cache" / "uv"),
            "UV_PYTHON_DOWNLOADS": "never",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "VLLM_TARGET_DEVICE": "empty",
            "COMPILE_CUSTOM_KERNELS": "0",
            "VLLM_NO_USAGE_STATS": "1",
            "DO_NOT_TRACK": "1",
            "OMP_NUM_THREADS": "2",
            "XDG_CACHE_HOME": str(output / "cache"),
        }
    )
    return env


def git_value(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()


def verify_source(path: Path, identity: dict) -> dict:
    if git_value(path, "rev-parse", "HEAD") != identity["commit"]:
        raise ValueError("Source commit does not match the candidate")
    if git_value(path, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Source has local changes")
    if git_value(path, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Full source history is required for version computation")
    origin = git_value(path, "remote", "get-url", "origin")
    if origin != identity["origin"]:
        raise ValueError("Source origin does not match the candidate")
    return {"commit": identity["commit"], "origin": origin, "full_history": True}


def stop_command(process) -> None:
    """The installer owns this process group; never target a device-wide PID list."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    except ProcessLookupError:
        process.wait(timeout=10)


def run_step(
    name: str,
    command: list[str],
    output: Path,
    env: dict,
    report: dict,
    *,
    timeout: int | None = 1800,
) -> None:
    started = time.monotonic()
    step = {"name": name, "argv": command, "status": "fail"}
    report["steps"].append(step)
    print(f"Restore step: {name}", flush=True)
    with (output / f"{name}.log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
            env=env,
            start_new_session=True,
        )
        try:
            step["returncode"] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_command(process)
            step["error"] = "timeout"
            raise TimeoutError(f"Restore step timed out: {name}") from None
        except BaseException:
            stop_command(process)
            raise
        finally:
            step["seconds"] = round(time.monotonic() - started, 3)
    if step["returncode"]:
        raise RuntimeError(f"Restore step failed: {name}; see private log")
    step["status"] = "pass"


def uv_install(uv: Path, python: Path, manifest: dict, *args: str) -> list[str]:
    return [
        str(uv),
        "--no-config",
        "pip",
        "install",
        "--python",
        str(python),
        "--index-url",
        manifest["indexes"][0],
        "--extra-index-url",
        manifest["indexes"][1],
        "--index-strategy",
        "unsafe-best-match",
        *args,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--core-source", type=Path, required=True)
    parser.add_argument("--ascend-source", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--port", type=int, default=18220)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        parser.error("Execution requires Linux aarch64")
    if args.device < 0 or not 0 < args.port < 65536:
        parser.error("Invalid logical device or loopback port")
    try:
        manifest = read_manifest()
        output = private_path(args.output)
        sources = {
            "core": private_path(args.core_source),
            "ascend": private_path(args.ascend_source),
        }
        if any(
            source == output or source in output.parents for source in sources.values()
        ):
            raise ValueError("Restore output cannot be within a source checkout")
        if output.exists():
            raise ValueError(
                "Restore output already exists; preserve previous evidence"
            )
        if len(os.fsencode(output / "smoke" / "kv.sock")) > 100:
            raise ValueError("Restore output is too long for the private IPC socket")
        if shutil.disk_usage(ROOT / "artifacts").free < 40 * 1024**3:
            raise ValueError("Restore requires at least 40 GiB free in project storage")
        python, uv = args.python.resolve(strict=True), args.uv.resolve(strict=True)
        if not python.is_file() or not uv.is_file():
            raise ValueError("Python and uv must be files")
        if platform.python_version() != manifest["python"]:
            raise ValueError("Use the recorded Python for this restore controller")
        version = subprocess.run(
            [str(python), "-I", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        if version != "Python " + manifest["python"]:
            raise ValueError("Base Python version does not match")
        version = subprocess.run(
            [str(uv), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.split()
        if version[:2] != ["uv", manifest["uv"]]:
            raise ValueError("uv version does not match")
        if not Path(manifest["cann_python_path"]).is_dir():
            raise ValueError("Recorded CANN Python path is missing")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
        model = args.model.resolve(strict=True)
        model_config = (model / "config.json").read_bytes()
        if output == model or model in output.parents:
            raise ValueError("Output cannot be within the model directory")
        identities = {
            role: verify_source(path, manifest["sources"][role])
            for role, path in sources.items()
        }
        output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    report = {
        "schema_version": 1,
        "scope": manifest["scope"],
        "started_at": datetime.now(UTC).isoformat(),
        "status": "fail",
        "sources": identities,
        "locks": manifest["locks"],
        "model_config_sha256": hashlib.sha256(model_config).hexdigest(),
        "model_revision": None,
        "image_digest": None,
        "steps": [],
        "environment_gate_passed": False,
        "kv_events_validated": False,
        "performance_validated": False,
    }
    env = build_environment(output, manifest["cann_python_path"])
    target = output / "service-venv"
    binary = target / "bin" / "python"
    lock = CANDIDATES / "hust-legacy-20260831-py312-aarch64.txt"
    try:
        for role, source in sources.items():
            destination = output / role
            run_step(
                f"clone-{role}",
                ["git", "clone", "--no-hardlinks", str(source), str(destination)],
                output,
                env,
                report,
                timeout=120,
            )
            run_step(
                f"pin-{role}",
                [
                    "git",
                    "-C",
                    str(destination),
                    "checkout",
                    "--detach",
                    identities[role]["commit"],
                ],
                output,
                env,
                report,
                timeout=60,
            )
        run_step(
            "venv",
            [str(uv), "--no-config", "venv", "--python", str(python), str(target)],
            output,
            env,
            report,
            timeout=60,
        )
        run_step(
            "dependencies",
            uv_install(uv, binary, manifest, "--require-hashes", "-r", str(lock)),
            output,
            env,
            report,
        )
        run_step(
            "triton-overlay",
            uv_install(
                uv,
                binary,
                manifest,
                "--require-hashes",
                "--no-deps",
                "--reinstall-package",
                "triton-ascend",
                "-r",
                str(lock),
            ),
            output,
            env,
            report,
        )
        run_step(
            "build-core",
            uv_install(
                uv,
                binary,
                manifest,
                "--no-deps",
                "--no-build-isolation",
                "-e",
                str(output / "core"),
            ),
            output,
            env,
            report,
        )
        run_step(
            "build-ascend",
            uv_install(
                uv,
                binary,
                manifest,
                "--no-deps",
                "--build-constraints",
                str(CANDIDATES / "ascend-hust-build.txt"),
                "-e",
                str(output / "ascend"),
            ),
            output,
            env,
            report,
        )
        for name, tool, options in (
            (
                "audit",
                "audit_runtime.py",
                ["--ascend-distribution", "vllm-ascend-hust"],
            ),
            ("overlay-audit", "verify_triton_overlay.py", []),
            ("imports", "inspect_runtime.py", ["--execute"]),
        ):
            run_step(
                name,
                [
                    str(binary),
                    str(ROOT / "tools" / tool),
                    *options,
                    "--output",
                    str(output / f"{name}.json"),
                ],
                output,
                env,
                report,
                timeout=120,
            )
        imported = json.loads((output / "imports.json").read_text("utf-8"))
        for module, role in (("vllm", "core"), ("vllm_ascend", "ascend")):
            if not Path(imported["modules"][module]).is_relative_to(output / role):
                raise ValueError("Imported engine is outside the restored source")
        # smoke_backend manages its own bounded HTTP/startup deadlines and its
        # separate engine process group. Do not kill its controller externally.
        run_step(
            "smoke",
            [
                str(binary),
                str(ROOT / "tools" / "smoke_backend.py"),
                "--execute",
                "--executable",
                str(target / "bin" / "vllm"),
                "--model",
                str(model),
                "--device",
                str(args.device),
                "--port",
                str(args.port),
                "--kv-events-ipc",
                "--output",
                str(output / "smoke"),
            ],
            output,
            env,
            report,
            timeout=None,
        )
        if any(git_value(output / role, "status", "--porcelain") for role in sources):
            raise ValueError("Source changed during the build")
        report["status"] = "pass"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        with (output / "restore.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(f"HUST candidate restore: {report['status']}; {output / 'restore.json'}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
