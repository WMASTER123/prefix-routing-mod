"""Run the fixed-Router in-process owner probe against loopback origins."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from threading import Lock

from tests.fixtures.mock_backend import Handler, mock_backend
from tools.smoke_backend import stop_owned_group

MAX_LOG_BYTES = 1_048_576


def run(binary: Path, output: Path) -> dict:
    if os.name != "posix":
        raise ValueError("runtime owner probe requires Linux process groups")
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("probe binary must be executable")
    output.mkdir(parents=True, exist_ok=False)
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"status": "fail", "binary_sha256": digest}
    process = None
    counts = {"worker_a": {}, "worker_b": {}}
    carriers = []
    lock = Lock()

    def handler_for(worker: str):
        class ProbeHandler(Handler):
            def before_reply(self, request):
                model = request["model"]
                with lock:
                    worker_counts = counts[worker]
                    worker_counts[model] = worker_counts.get(model, 0) + 1
                    carriers.append(self.headers.get("X-Request-Id"))

        return ProbeHandler

    try:
        with (
            mock_backend(handler_for("worker_a")) as worker_a,
            mock_backend(handler_for("worker_b")) as worker_b,
        ):
            with (output / "probe.log").open("xb") as log:
                process = subprocess.Popen(
                    [str(binary), worker_a, worker_b],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                process.wait(timeout=30)
            if process.returncode:
                raise RuntimeError(f"probe exited {process.returncode}")
            payload = (output / "probe.log").read_bytes()
            if len(payload) > MAX_LOG_BYTES:
                raise ValueError("probe log exceeded evidence budget")
            records = [
                json.loads(line)
                for line in payload.decode().splitlines()
                if line.startswith('{"status":')
            ]
            if len(records) != 1 or records[0].get("status") != "pass":
                raise ValueError("missing bounded probe result")
            with lock:
                report["backend_attempts"] = counts
                report["router_owned_carriers"] = carriers
            if counts != {
                "worker_a": {"runtime-base": 1},
                "worker_b": {"runtime-cache": 1},
            }:
                raise ValueError("runtime selection did not reach expected origin")
            if len(carriers) != 2 or any(
                carrier is None or not carrier.startswith("prm-process-a-")
                for carrier in carriers
            ):
                raise ValueError("owner attempt carrier missing")
            report["probe"] = records[0]
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
                report["cleanup_error"] = f"{type(error).__name__}: {error}"
                report["status"] = "fail"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.binary, args.output)
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
