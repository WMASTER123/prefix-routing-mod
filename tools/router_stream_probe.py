"""Manual native Router stream resource probe against owned loopback fixtures."""

import argparse
import hashlib
import json
import os
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from threading import Lock

from tests.fixtures.mock_backend import Handler, mock_backend
from tools.router_extension_probe import prototype_result
from tools.smoke_backend import stop_owned_group


def run(binary: Path, output: Path) -> dict:
    if os.name != "posix":
        raise ValueError("native stream probe requires Linux process groups")
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("native binary must be executable")
    output.mkdir(parents=True, exist_ok=False)
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"status": "fail", "binary_sha256": digest}
    counts, lock = {}, Lock()
    process = None

    class StreamHandler(Handler):
        def before_reply(self, request):
            self.fixture_model = request["model"]
            with lock:
                counts[self.fixture_model] = counts.get(self.fixture_model, 0) + 1

        def reply(self, status, body, content_type="application/json"):
            if getattr(self, "fixture_model", None) == "fixture-large":
                body = b"x" * 16384
            with suppress(BrokenPipeError, ConnectionResetError):
                if getattr(self, "fixture_model", None) == "fixture-stream-stall":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(0.5)
                    self.wfile.write(body)
                    self.wfile.flush()
                else:
                    super().reply(status, body, content_type)

    try:
        with mock_backend(StreamHandler) as a, mock_backend(StreamHandler) as b:
            with (output / "prototype.log").open("wb") as log:
                process = subprocess.Popen(
                    [str(binary), a, b],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                process.wait(timeout=30)
            if process.returncode:
                raise RuntimeError(f"stream prototype exited {process.returncode}")
            report["prototype"] = prototype_result(output / "prototype.log")
            report["backend_attempts"] = dict(counts)
            if counts != dict.fromkeys(
                ["fixture", "fixture-large", "fixture-stream-stall", "fixture-drop"], 2
            ):
                raise ValueError("unexpected resend or missing stream branch")
            report["status"] = "pass"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if process is not None:
            try:
                report["cleanup"] = stop_owned_group(process)
                if report["cleanup"]["forced_group_cleanup"]:
                    report["status"] = "fail"
            except Exception as error:
                report["status"] = "fail"
                report["cleanup_error"] = f"{type(error).__name__}: {error}"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.binary, args.output)
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
