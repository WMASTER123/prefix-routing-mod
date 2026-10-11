"""Opt-in, bounded single-backend probe; not the full NPU routing test suite.

Launch only the supplied local vLLM executable, preserve the system environment,
and clean up only its dedicated process group. No engine imports at module load.
"""

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

MODEL_NAME = "prefix-qwen14b-probe"
PLUGINS = "ascend,ascend_kv_connector,ascend_model,ascend_model_loader"


def build_command(
    executable: Path,
    model: Path,
    port: int,
    events_port: int | None = None,
    *,
    events_ipc: Path | None = None,
) -> list[str]:
    if events_port is not None and events_ipc is not None:
        raise ValueError("Choose one KV event transport")
    command = [
        str(executable),
        "serve",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--served-model-name",
        MODEL_NAME,
        "--generation-config",
        "vllm",
        "--shutdown-timeout",
        "10",
        "--tensor-parallel-size",
        "1",
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "8192",
        "--max-num-seqs",
        "8",
        "--max-num-batched-tokens",
        "2048",
        "--gpu-memory-utilization",
        "0.70",
        "--enable-prefix-caching",
        "--enable-chunked-prefill",
        "--enforce-eager",
        "--additional-config",
        '{"enable_cpu_binding":false}',
    ]
    if events_port is not None or events_ipc is not None:
        command.extend(
            [
                "--kv-events-config",
                json.dumps(
                    {
                        "enable_kv_cache_events": True,
                        "publisher": "zmq",
                        "endpoint": (
                            ipc_endpoint(events_ipc)
                            if events_ipc is not None
                            else f"tcp://127.0.0.1:{events_port}"
                        ),
                        "hwm": 128,
                        "max_queue_size": 128,
                        "buffer_steps": 128,
                    }
                ),
            ]
        )
    return command


def ipc_endpoint(path: Path) -> str:
    # Conservative Linux sockaddr_un limit; generated inside a fresh run dir.
    if not path.is_absolute() or len(os.fsencode(path)) > 100:
        raise ValueError("IPC socket needs an absolute path of at most 100 bytes")
    return f"ipc://{path}"


def create_event_subscriber(context, zmq, port: int | None, ipc: Path | None = None):
    if (port is None) == (ipc is None):
        raise ValueError("Choose exactly one KV event transport")
    subscriber = context.socket(zmq.SUB)
    subscriber.setsockopt(zmq.LINGER, 0)
    subscriber.setsockopt(zmq.RCVHWM, 128)
    subscriber.setsockopt(zmq.SUBSCRIBE, b"")
    try:
        # The fixed backend connects PUB for non-wildcard TCP endpoints.
        # Bind SUB to loopback; connect/connect would silently emit nothing.
        if ipc is not None:
            subscriber.connect(ipc_endpoint(ipc))
        else:
            subscriber.bind(f"tcp://127.0.0.1:{port}")
    except Exception:
        subscriber.close(linger=0)
        raise
    return subscriber


def summarize_event_batch(batch: list) -> list[dict]:
    """Summarize known upstream arrays and HUST tagged maps, not an index.

    No token IDs, hashes or prompt content are persisted. This deliberately
    does not assert replay, ordering, readiness, removal or residency coverage.
    """
    if not isinstance(batch, list) or len(batch) < 2 or not isinstance(batch[1], list):
        raise ValueError("Unexpected event batch envelope")
    summaries = []
    for event in batch[1]:
        if isinstance(event, dict):
            kind = event.get("type")
            fields = event
        elif isinstance(event, list) and event and isinstance(event[0], str):
            kind = event[0]
            fields = {}
            if kind == "BlockStored" and len(event) >= 8:
                fields = dict(
                    zip(
                        (
                            "block_hashes",
                            "parent_block_hash",
                            "token_ids",
                            "block_size",
                            "lora_id",
                            "medium",
                            "lora_name",
                        ),
                        event[1:8],
                        strict=True,
                    )
                )
        else:
            raise ValueError("Unexpected event encoding")
        if not isinstance(kind, str) or kind not in {
            "BlockStored",
            "BlockRemoved",
            "AllBlocksCleared",
        }:
            raise ValueError("Unknown native event kind")
        summary = {"kind": kind}
        if kind == "BlockStored":
            if (
                not isinstance(fields.get("block_hashes"), list)
                or not isinstance(fields.get("token_ids"), list)
                or type(fields.get("block_size")) is not int
                or fields["block_size"] <= 0
                or "parent_block_hash" not in fields
                or "medium" not in fields
                or (
                    fields["medium"] is not None
                    and not isinstance(fields["medium"], str)
                )
            ):
                raise ValueError("Incomplete BlockStored payload")
            summary.update(
                {
                    "block_count": len(fields["block_hashes"]),
                    "token_count": len(fields["token_ids"]),
                    "block_size": fields["block_size"],
                    "parent_present": fields["parent_block_hash"] is not None,
                    "medium": fields["medium"],
                }
            )
        summaries.append(summary)
    return summaries


def parse_stream(lines) -> dict:
    chunks = 0
    content_chars = 0
    done = False
    finished = False
    for line in lines:
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            done = True
            break
        message = json.loads(payload)
        if "error" in message:
            raise ValueError("Streaming backend returned an error")
        for choice in message.get("choices", []):
            text = choice.get("text", "")
            if text:
                chunks += 1
                content_chars += len(text)
            finished |= choice.get("finish_reason") is not None
    if not (done and finished and content_chars):
        raise ValueError("Stream lacks content, finish reason or DONE")
    return {"content_chunks": chunks, "content_chars": content_chars, "done": done}


def request(opener, origin: str, prompt: str, stream: bool):
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "temperature": 0,
        "max_tokens": 24,
        "stream": stream,
    }
    request = urllib.request.Request(
        origin + "/v1/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=120) as response:
        if stream:
            return parse_stream(response)
        result = json.load(response)
    if "error" in result or not result.get("choices"):
        raise ValueError("Completion has no choices or contains an error")
    choice = result["choices"][0]
    if not choice.get("text") or choice.get("finish_reason") is None:
        raise ValueError("Completion lacks text or finish reason")
    return {"usage": result.get("usage"), "content_chars": len(choice["text"])}


def stop_owned_group(process: subprocess.Popen) -> dict:
    # start_new_session makes the child's PID its process-group ID. This helper
    # must never accept an arbitrary external PID or kill by executable name.
    forced = False
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.terminate()  # Let the API server close its engine first.
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        forced = True
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    # The parent may exit before its engine workers. Terminate any survivors
    # in this probe's private group, not other services owned by the account.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, 0)
        forced = True
        os.killpg(process.pid, signal.SIGKILL)
    return {"parent_returncode": process.returncode, "forced_group_cleanup": forced}


def build_environment(device: int, output: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Do not disable bytecode: cold Torch/NPU parsing can exhaust HUST's 20s
    # preflight. Redirect all generated bytecode away from system/shared paths.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env.update(
        {
            "ASCEND_RT_VISIBLE_DEVICES": str(device),
            "VLLM_PLUGINS": PLUGINS,
            "PYTHONPYCACHEPREFIX": str(output / "cache" / "pyc"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "VLLM_NO_USAGE_STATS": "1",
            "DO_NOT_TRACK": "1",
            "OMP_NUM_THREADS": "2",
            "TOKENIZERS_PARALLELISM": "false",
            "XDG_CACHE_HOME": str(output / "cache"),
            "VLLM_CACHE_ROOT": str(output / "cache" / "vllm"),
        }
    )
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--port", type=int, default=18140)
    events = parser.add_mutually_exclusive_group()
    events.add_argument("--kv-events-port", type=int)
    events.add_argument("--kv-events-ipc", action="store_true")
    parser.add_argument("--startup-timeout", type=int, default=600)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("Execution requires Linux; use CPU tests elsewhere")
    if not (0 < args.port < 65536 and args.device >= 0):
        parser.error("Invalid port or logical Ascend device")
    if args.kv_events_port is not None and (
        not 0 < args.kv_events_port < 65536 or args.kv_events_port == args.port
    ):
        parser.error("KV events port must be valid and distinct from HTTP")
    if not 1 <= args.startup_timeout <= 1800:
        parser.error("Startup timeout must be between 1 and 1800 seconds")
    executable = args.executable.resolve(strict=True)
    model = args.model.resolve(strict=True)
    if not executable.is_file() or not (model / "config.json").is_file():
        parser.error("Executable and model/config.json must exist")
    output = args.output.resolve()
    if output == model or model in output.parents:
        parser.error("Never write reports into the model directory")
    # Refuse overwrites, including a prior interrupted run's logs.
    events_ipc = output / "kv.sock" if args.kv_events_ipc else None
    if events_ipc is not None:
        try:
            ipc_endpoint(events_ipc)
        except ValueError as error:
            parser.error(str(error))
    output.mkdir(parents=True, exist_ok=False)
    origin = f"http://127.0.0.1:{args.port}"
    command = build_command(
        executable, model, args.port, args.kv_events_port, events_ipc=events_ipc
    )
    report = {
        "schema_version": 1,
        "scope": "single_backend_eager_probe",
        "status": "fail",
        "command": command,
        "device_mask": str(args.device),
        "plugins": PLUGINS,
        "event_transport": "ipc"
        if events_ipc
        else "tcp"
        if args.kv_events_port
        else None,
        "checks": {},
        "performance_validated": False,
        "routing_validated": False,
        "kv_events_validated": False,
        "shutdown_validated": False,
    }
    process = None
    event_socket = None
    context = None
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", args.port))
        if args.kv_events_port is not None or events_ipc is not None:
            # Available in the probed engine environment, not package dependencies.
            import msgspec
            import zmq

            if args.kv_events_port is not None:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", args.kv_events_port))
            context = zmq.Context()
            event_socket = create_event_subscriber(
                context, zmq, args.kv_events_port, events_ipc
            )
        env = build_environment(args.device, output)
        # Ignore inherited HTTP proxy configuration for loopback requests.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with (output / "backend.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=output,
                start_new_session=True,
            )
            report["pid"] = process.pid
            deadline = time.monotonic() + args.startup_timeout
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Backend exited with {process.returncode}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Backend readiness timed out")
                try:
                    with opener.open(origin + "/health", timeout=2) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(1)
            report["checks"]["health"] = True
            prompt = "The routing experiment uses a shared prefix. " * 80
            prompt += "\nWrite one sentence about efficient inference:"
            report["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
            for label, streaming in (
                ("first", False),
                ("repeat", False),
                ("stream", True),
            ):
                report["checks"][label] = request(opener, origin, prompt, streaming)
            with opener.open(origin + "/metrics", timeout=10) as response:
                metrics = response.read().decode()
            (output / "metrics.prom").write_text(metrics, encoding="utf-8")
            if event_socket is not None:
                batches = []
                for _ in range(256):
                    if not event_socket.poll(1000):
                        break
                    frames = event_socket.recv_multipart()
                    if len(frames) != 3 or len(frames[1]) != 8:
                        raise ValueError("Unexpected ZMQ event framing")
                    events = summarize_event_batch(msgspec.msgpack.decode(frames[2]))
                    batches.append(
                        {"sequence": int.from_bytes(frames[1], "big"), "events": events}
                    )
                report["checks"]["native_event_samples"] = batches
                if not any(
                    e["kind"] == "BlockStored" for b in batches for e in b["events"]
                ):
                    raise ValueError("No native BlockStored event observed")
            if process.poll() is not None:
                raise RuntimeError("Backend exited during requests")
            report["status"] = "pass"
    except (OSError, ValueError, RuntimeError, TimeoutError, ImportError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if process is not None:
            try:
                report["cleanup"] = stop_owned_group(process)
            except (OSError, subprocess.TimeoutExpired) as error:
                report["status"] = "fail"
                report["cleanup_error"] = str(error)
        if event_socket is not None:
            event_socket.close(linger=0)
        if context is not None:
            context.term()
        (output / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
