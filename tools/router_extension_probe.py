"""Manual CPU prototype: actual Router library + reused loopback fixture.

No production proxy, tokenizer, cache events or reliable backend receipt here.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
from contextlib import nullcontext, suppress
from pathlib import Path
from threading import Lock

from tests.fixtures.mock_backend import Handler, mock_backend
from tools.lifecycle_trace_probe import validate_log
from tools.online_transport_probe import online_receiver
from tools.smoke_backend import stop_owned_group

MAX_LOG_BYTES = 1_048_576


def prototype_result(path: Path) -> dict:
    with path.open("rb") as stream:
        payload = stream.read(MAX_LOG_BYTES + 1)
    if len(payload) > MAX_LOG_BYTES:
        raise ValueError("prototype log exceeded evidence budget")
    records = [
        json.loads(line)
        for line in payload.decode().splitlines()
        if line.startswith('{"status":')
    ]
    if len(records) != 1 or records[0].get("status") != "pass":
        raise ValueError("missing bounded prototype result")
    return records[0]


def run(binary: Path, output: Path, *, online=False) -> dict:
    if os.name != "posix":
        raise ValueError("prototype probe requires Linux process groups")
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("prototype binary must be executable")
    output.mkdir(parents=True, exist_ok=False)
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"status": "fail", "binary_sha256": digest}
    process = None
    lock = Lock()
    counts = {}
    disabled_environment = dict(os.environ)
    disabled_environment.pop("PREFIX_PROBE_PORT", None)
    disabled_environment.pop("PREFIX_PROBE_TOKEN", None)

    class ProbeHandler(Handler):
        def before_reply(self, request):
            model = request["model"]
            with lock:
                counts[model] = counts.get(model, 0) + 1
            if model == "fixture-stall":
                time.sleep(3)
            elif model == "fixture-disconnect":
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)

        def reply(self, *args, **kwargs):
            # Expected after cancellation; not backend-ended proof.
            with suppress(BrokenPipeError, ConnectionResetError):
                super().reply(*args, **kwargs)

    try:
        with (
            online_receiver(
                output / "receipts.sqlite",
                lambda: process is not None and process.poll() is None,
            )
            if online
            else nullcontext(None) as channel,
            mock_backend(ProbeHandler) as worker_a,
            mock_backend(ProbeHandler) as worker_b,
        ):
            with (output / "prototype.log").open("wb") as log:
                process = subprocess.Popen(
                    [str(binary), worker_a, worker_b],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=channel[0] if channel else disabled_environment,
                )
                process.wait(timeout=30)
            if process.returncode:
                raise RuntimeError(f"prototype exited {process.returncode}")
            result = prototype_result(output / "prototype.log")
            report["lifecycle_replay"] = validate_log(output / "prototype.log")
            with lock:
                report["backend_attempts"] = dict(counts)
            if report["backend_attempts"] != {
                "fixture": 21,
                "fixture-error": 2,
                "fixture-stall": 1,
                "fixture-fallback": 1,
                "fixture-disconnect": 1,
            }:
                raise ValueError("unexpected resend or failed send-fence isolation")
            report["prototype"] = result
            report["status"] = "pass"
        if online:
            report["online_transport"] = channel[1]
            observed = report["online_transport"]
            if not (
                observed["receipts"] == 46
                and observed["online_applied_while_producer_alive"] == 46
                and observed["transport_unknown"] == 22
                and observed["not_sent"] == 2
                and observed["unused_fixture_reservations"] == 40
                and observed["outstanding_including_unused"] == 62
                and observed["rejected_connections"] == 0
                and observed["admission_ready"] is False
            ):
                raise ValueError("online receipt/debt mismatch")
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
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    report = run(args.binary, args.output, online=args.online)
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
