"""One-card cedb477 request and native-event qualification, explicitly invoked.

Reuses the owned process supervisor and HTTP/IPC helpers. Native END_SEQ does
not establish a complete cache snapshot. No production directory is installed.
"""

import argparse
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import urllib.request
from pathlib import Path

from prefix_routing_mod.adapters.kv import native_live_batch, native_replay_batch
from prefix_routing_mod.adapters.profiles import inspect_backend
from tools.regress_kv_identity import post
from tools.regress_kv_recovery import OwnedBackend
from tools.restore_runtime import private_path
from tools.smoke_backend import build_command, build_environment, ipc_endpoint

PROFILE = "hust-cedb477-native-v1"


def command(executable, model, port, output):
    argv = build_command(executable, model, port, events_ipc=output / "events.sock")
    for flag, value in (
        ("--dtype", "float16"),
        ("--max-model-len", "1024"),
        ("--max-num-seqs", "2"),
        ("--max-num-batched-tokens", "1024"),
    ):
        argv[argv.index(flag) + 1] = value
    config_at = argv.index("--kv-events-config") + 1
    config = json.loads(argv[config_at])
    config.update(replay_endpoint=ipc_endpoint(output / "replay.sock"), buffer_steps=16)
    argv[config_at] = json.dumps(config)
    argv.extend(
        [
            "--seed",
            "0",
            "--prefix-caching-hash-algo",
            "sha256",
            "--block-size",
            "128",
            "--enable-prompt-tokens-details",
        ]
    )
    return argv


def cached_tokens(usage):
    details = usage.get("prompt_tokens_details")
    value = details.get("cached_tokens") if isinstance(details, dict) else None
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Invalid backend cached-token count")
    return value


def run(args, output, report):
    report["interface"] = inspect_backend(PROFILE)
    report["model_assets_sha256"] = {
        name: hashlib.sha256((args.model / name).read_bytes()).hexdigest()
        for name in (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "chat_template.jinja",
        )
        if (args.model / name).is_file()
    }
    candidate = json.loads(
        (
            Path(__file__).resolve().parents[1] / "runtime/candidates/hust-cedb477.json"
        ).read_text("utf-8")
    )
    acl = Path(candidate["cann_acl_path"])
    if (
        hashlib.sha256((acl / "acl.so").read_bytes()).hexdigest()
        != candidate["cann_acl_sha256"]
    ):
        raise ValueError("CANN ACL source fingerprint differs")
    import msgspec
    import zmq
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    prompt = "A reusable prefix supports efficient inference. " * 64
    tokens = tokenizer.encode(prompt, add_special_tokens=False)
    if not 256 <= len(tokens) <= 900:
        raise ValueError("Probe prompt outside the bounded scope")
    messages = [{"role": "user", "content": prompt}]
    chat_tokens = tokenizer.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True, return_dict=False
    )
    report["input_tokens"] = {"completion": len(tokens), "chat": len(chat_tokens)}
    env = build_environment(
        args.device, output, pycache_root=getattr(args, "pycache_root", None)
    )
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "PREFIX_ROUTING_HUST_PUBLISHER",
        "PREFIX_ROUTING_HUST_TERMINAL",
        "PREFIX_ROUTING_TERMINAL_TOKEN",
    ):
        env.pop(name, None)
    env.update(
        PYTHONPATH=str(acl),
        PYTHONHASHSEED="0",
        VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES="0",
        VLLM_USE_FASTOKENS="0",
        VLLM_USE_RUST_FRONTEND="0",
    )
    terminal_token = None
    if getattr(args, "terminal_observer", False):
        terminal_token = secrets.token_hex(32)
        env.update(
            PREFIX_ROUTING_HUST_TERMINAL="1",
            PREFIX_ROUTING_TERMINAL_TOKEN=terminal_token,
            PREFIX_ROUTING_TERMINAL_PREFIX="prm-",
            PREFIX_ROUTING_TERMINAL_WORKER="replica-a",
        )
        env["VLLM_PLUGINS"] += ",prefix_routing_terminal"
    origin = f"http://127.0.0.1:{args.port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    context = zmq.Context()
    sub = context.socket(zmq.SUB)
    replay = context.socket(zmq.DEALER)
    for sock in (sub, replay):
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.MAXMSGSIZE, 1024**2)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVHWM, 128)
    sub.connect(ipc_endpoint(output / "events.sock"))
    replay.connect(ipc_endpoint(output / "replay.sock"))
    argv = command(
        Path(sys.executable).with_name("vllm"), args.model, args.port, output
    )
    backend = OwnedBackend(argv, env, output / "backend", opener, origin)
    report["command"] = argv
    try:
        backend.start()
        results = []
        for route, body, expected in (
            ("/v1/completions", {"prompt": prompt}, len(tokens)),
            ("/v1/completions", {"prompt": prompt}, len(tokens)),
            ("/v1/chat/completions", {"messages": messages}, len(chat_tokens)),
        ):
            result = post(
                opener,
                origin,
                route,
                {
                    "model": "prefix-qwen14b-probe",
                    "max_tokens": 16,
                    "temperature": 0,
                    **body,
                },
            )
            if result["usage"]["prompt_tokens"] != expected or not result.get(
                "choices"
            ):
                raise ValueError("Request rendering/count differs")
            usage = result["usage"]
            cached = cached_tokens(usage)
            results.append({"id": result["id"], "route": route, "usage": usage})
            if len(results) == 2 and (type(cached) is not int or cached <= 0):
                raise ValueError("Repeated request lacks reported cache reuse")
        if len({item["id"] for item in results}) != len(results):
            raise ValueError("Duplicate response IDs")
        report["requests"] = results
        live = []
        while len(live) < 128 and sub.poll(1000):
            live.append(
                native_live_batch(
                    sub.recv_multipart(),
                    msgspec.msgpack.decode,
                    profile_id=PROFILE,
                    block_size=128,
                )
            )
        if not live or live[0]["sequence"] != 0:
            raise ValueError("Native stream did not cover the known-empty start")
        if [item["sequence"] for item in live] != list(range(len(live))):
            raise ValueError("Native live sequence gap")
        stored = [op for item in live for op in item["ops"] if op["kind"] == "store"]
        if not stored:
            raise ValueError("No native stored event normalized")
        replay.send_multipart([b"", (0).to_bytes(8, "big")])
        replayed = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not replay.poll(100):
                continue
            item = native_replay_batch(
                replay.recv_multipart(),
                msgspec.msgpack.decode,
                profile_id=PROFILE,
                block_size=128,
            )
            if item is None:
                break
            if len(replayed) >= 128:
                raise ValueError("Replay exceeds probe budget")
            replayed.append(item)
        else:
            raise TimeoutError("Native replay END_SEQ missing")
        if replayed != live:
            raise ValueError("Idle native replay differs from live observations")
        report["events"] = {
            "live_batches": len(live),
            "replay_batches": len(replayed),
            "stored_blocks": len(stored),
            "normalization_verified": True,
            "idle_replay_matches": True,
        }
        # Only transient normalized keys feed the existing index experiment.
        (output / "normalized.json").write_text(json.dumps(live), "utf-8")
        if terminal_token is not None:
            from tools.live_terminal_cases import run as terminal_cases

            report["terminal"] = terminal_cases(opener, origin, output, terminal_token)
            report["terminal_observer_verified"] = (
                report["terminal"]["status"] == "pass"
            )
        report["status"] = "pass"
    finally:
        try:
            backend.stop()
            report["cleanup"] = backend.cleanup
        finally:
            sub.close()
            replay.close()
            context.term()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--port", type=int, default=18146)
    parser.add_argument("--terminal-observer", action="store_true")
    parser.add_argument("--pycache-root", type=Path)
    args = parser.parse_args(argv)
    try:
        output = private_path(args.output)
        args.model = args.model.resolve(strict=True)
        if (
            os.name != "posix"
            or not 0 <= args.device <= 31
            or not 0 < args.port < 65536
        ):
            raise ValueError("Invalid platform/device/port")
        if output == args.model or args.model in output.parents:
            raise ValueError("Output cannot be inside the model directory")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", args.port))
        output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    report = {
        "status": "fail",
        "profile_id": PROFILE,
        "device": args.device,
        "scope": "single-backend-fp16-eager-native-subset",
        "routing_verified": False,
        "full_recovery_verified": False,
        "terminal_observer_verified": False,
        "performance_verified": False,
    }
    try:
        run(args, output, report)
    except Exception as exc:
        report["status"] = "fail"
        report["error"] = f"{type(exc).__name__}: {exc}"
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "error": report.get("error"),
            }
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
