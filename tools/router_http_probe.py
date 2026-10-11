"""Manual native Router admission against the existing loopback backend fixture.

Run as python -m tools.router_http_probe. No model or device is initialized.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from tests.fixtures.mock_backend import Handler, mock_backend
from tools.smoke_backend import stop_owned_group

MAX_RESPONSE_BYTES = 16_384


def exchange(opener, url, payload=None):
    request = Request(
        url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        response = opener.open(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("fixture response exceeded budget")
        return response.status, response.headers.get("Content-Type", ""), body


def run(binary: Path, output: Path) -> dict:
    if os.name != "posix":
        raise ValueError("native Router probe requires Linux process groups")
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Router binary must be an executable file")
    output.mkdir(parents=True, exist_ok=False)
    with binary.open("rb") as stream:
        binary_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"status": "fail", "binary_sha256": binary_hash, "checks": []}
    count_lock = Lock()
    posts = 0

    class CountedHandler(Handler):
        def do_POST(self):
            nonlocal posts
            with count_lock:
                posts += 1
            super().do_POST()

    process = None
    opener = build_opener(ProxyHandler({}))
    try:
        with mock_backend(CountedHandler) as worker:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            command = [
                str(binary),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--worker-urls",
                worker,
                "--policy",
                "round_robin",
                "--disable-retries",
                "--worker-startup-timeout-secs",
                "10",
                "--worker-startup-check-interval",
                "1",
                "--request-timeout-secs",
                "5",
            ]
            report["arguments"] = command[1:]
            with (output / "router.log").open("wb") as log:
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                origin = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Router exited before readiness")
                    try:
                        status, _, _ = exchange(opener, origin + "/health")
                        if status == 200:
                            break
                    except (URLError, TimeoutError, ConnectionError):
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Router readiness exceeded 30 seconds")
                    time.sleep(0.1)
                report["checks"].append("native_startup_health")
                status_path = Path(f"/proc/{process.pid}/status")
                report["startup_memory"] = [
                    line
                    for line in status_path.read_text().splitlines()
                    if line.startswith(("VmRSS:", "VmHWM:", "Threads:"))
                ]
                for route, payload in (
                    ("/v1/completions", {"prompt": "fixture"}),
                    (
                        "/v1/chat/completions",
                        {"messages": [{"role": "user", "content": "fixture"}]},
                    ),
                ):
                    for stream in (False, True):
                        status, content_type, body = exchange(
                            opener,
                            origin + route,
                            {
                                "model": "fixture",
                                "max_tokens": 4,
                                "stream": stream,
                                **payload,
                            },
                        )
                        if status != 200:
                            raise RuntimeError(f"{route} returned {status}")
                        if stream:
                            if "text/event-stream" not in content_type:
                                raise ValueError("SSE content type lost")
                            if body.count(b"data: [DONE]") != 1:
                                raise ValueError("SSE terminal framing changed")
                        elif (
                            json.loads(body)["choices"][0]["message"]["content"]
                            != "hello world"
                        ):
                            raise ValueError("JSON response changed")
                        report["checks"].append(f"{route}:stream={stream}")
                before = posts
                status, _, _ = exchange(
                    opener,
                    origin + "/v1/completions",
                    {"model": "fixture-error", "prompt": "fixture", "max_tokens": 4},
                )
                if status != 503 or posts - before != 1:
                    raise ValueError(
                        "disabled retry did not preserve one backend attempt"
                    )
                report["checks"].append("503_one_backend_attempt")
                report["backend_posts"] = posts
                report["status"] = "pass"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if process is not None:
            try:
                report["cleanup"] = stop_owned_group(process)
                if report["cleanup"]["forced_group_cleanup"]:
                    report["status"] = "fail"
                    report["cleanup_error"] = "Router needed forced group cleanup"
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
