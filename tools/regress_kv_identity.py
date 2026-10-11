"""Opt-in Qwen/HUST token, template and full-block event correspondence probe."""

import argparse
import hashlib
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

if __package__:
    from . import kv_identity, regress_kv_events, restore_runtime, smoke_backend
    from .regress_kv_recovery import OwnedBackend
else:
    import kv_identity
    import regress_kv_events
    import restore_runtime
    import smoke_backend
    from regress_kv_recovery import OwnedBackend


def post(opener, origin, route, payload):
    request = urllib.request.Request(
        origin + route,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=120) as response:
        raw = response.read(1024**2 + 1)
    if len(raw) > 1024**2:
        raise ValueError("HTTP response exceeds probe bound")
    result = json.loads(raw)
    if not isinstance(result, dict) or "error" in result:
        raise ValueError("Backend returned an invalid result")
    return result


def run(args, output, report):
    import msgspec
    import zmq
    from transformers import AutoTokenizer
    from vllm.distributed import kv_events as native_events
    from vllm.utils.hashing import get_hash_fn_by_name

    if (
        not Path(native_events.__file__)
        .resolve()
        .is_relative_to(args.core_source.resolve(strict=True))
    ):
        raise ValueError("Imported event source differs from the audited source")
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    text = "Identity regression:\n" + "A shared prefix improves inference. " * 80
    tokens = tokenizer.encode(text, add_special_tokens=True)
    if not 256 <= len(tokens) <= 900:
        raise ValueError("Controlled prompt is outside the probe context")
    messages = [{"role": "user", "content": text}]
    chat_tokens = tokenizer.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True
    )
    assets = {}
    for name in (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "chat_template.jinja",
    ):
        asset = args.model / name
        if asset.is_file():
            assets[name] = hashlib.sha256(asset.read_bytes()).hexdigest()
    report["model_assets_sha256"] = assets
    report["input_tokens"] = len(tokens)
    origin = f"http://127.0.0.1:{args.port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    context = zmq.Context()
    subscriber = backend = None
    try:
        subscriber = smoke_backend.create_event_subscriber(
            context, zmq, None, output / "events.sock"
        )
        subscriber.setsockopt(zmq.MAXMSGSIZE, 1024**2)
        command = regress_kv_events.test_command(
            args.executable, args.model, args.port, output
        )
        env = smoke_backend.build_environment(
            args.device, output, pycache_root=args.pycache_root
        )
        env["PYTHONPATH"] = restore_runtime.read_manifest()["cann_python_path"]
        # Native defaults and process hash seed stay unchanged. This probe
        # verifies tokens and observed hash chains, not another process's seed.
        backend = OwnedBackend(command, env, output / "backend", opener, origin)
        backend.start()
        initial = regress_kv_events.replay(
            context,
            zmq,
            msgspec,
            smoke_backend.ipc_endpoint(output / "replay.sock"),
            0,
            force=True,
        )
        if initial["next_seq"] != 0:
            raise ValueError("Expected a supervised empty event origin")
        report["publisher_epoch"] = initial["publisher_epoch"]
        report["checks"]["completion_tokenizer"] = (
            post(
                opener,
                origin,
                "/tokenize",
                {
                    "model": smoke_backend.MODEL_NAME,
                    "prompt": text,
                    "add_special_tokens": True,
                },
            ).get("tokens")
            == tokens
        )
        report["checks"]["chat_template_tokens"] = (
            post(
                opener,
                origin,
                "/tokenize",
                {
                    "model": smoke_backend.MODEL_NAME,
                    "messages": messages,
                    "add_generation_prompt": True,
                    "add_special_tokens": False,
                },
            ).get("tokens")
            == chat_tokens
        )
        anchors, sequence = {}, 0
        hash_function = get_hash_fn_by_name("xxhash")
        report["requests"] = []
        for label, prompt in (("tokens", tokens), ("text_repeat", text)):
            result = post(
                opener,
                origin,
                "/v1/completions",
                {
                    "model": smoke_backend.MODEL_NAME,
                    "prompt": prompt,
                    "temperature": 0,
                    "max_tokens": 1,
                    "ignore_eos": True,
                },
            )
            if result.get("usage", {}).get("prompt_tokens") != len(tokens):
                raise ValueError("Backend prompt token count differs")
            if label == "tokens":
                deadline = time.monotonic() + 15
                observed = False
                while time.monotonic() < deadline and not observed:
                    if not subscriber.poll(100):
                        continue
                    frames = subscriber.recv_multipart()
                    if len(frames) != 3 or len(frames[1]) != 8:
                        raise ValueError("Invalid live event framing")
                    batch = msgspec.msgpack.decode(frames[2])
                    observed = any(
                        event["kind"] == "BlockStored"
                        for event in smoke_backend.summarize_event_batch(batch)
                    )
                if not observed:
                    raise TimeoutError("No real storage event observed")
            summaries = []
            # A control-plane replay gives an exclusive watermark without
            # requiring PUB idle time to prove that all batches were received.
            response = regress_kv_events.replay(
                context,
                zmq,
                msgspec,
                smoke_backend.ipc_endpoint(output / "replay.sock"),
                sequence,
                report.get("publisher_epoch"),
            )
            regress_kv_events.validate_response(
                response, sequence, report["publisher_epoch"]
            )
            if response.get("snapshot") is not None:
                raise ValueError("Token-bearing event history is unavailable")
            for _, payload in response["replayed_batches"]:
                batch = msgspec.msgpack.decode(payload)
                smoke_backend.summarize_event_batch(batch)
                for event in batch[1]:
                    if event["type"] == "BlockStored":
                        anchors, summary = kv_identity.check_stored_chain(
                            event, tokens, anchors, hash_function=hash_function
                        )
                        summaries.append(summary)
                    elif event["type"] == "BlockRemoved":
                        raise ValueError("Unexpected eviction in correspondence probe")
            sequence = response["next_seq"]
            report["requests"].append(
                {
                    "case": label,
                    "usage": result["usage"],
                    "stored": summaries,
                    "next_seq": sequence,
                }
            )
        report["checks"]["full_block_correspondence"] = bool(anchors)
        report["checks"]["native_chained_hashes"] = any(
            item["native_chained_hash_checks"] > 0
            for request in report["requests"]
            for item in request["stored"]
        )
        if not all(report["checks"].values()):
            raise ValueError("An identity check failed")
    finally:
        if backend is not None:
            backend.stop()
            report["cleanup"] = backend.cleanup
        if subscriber is not None:
            subscriber.close(linger=0)
        context.term()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--port", type=int, default=18320)
    parser.add_argument("--core-source", type=Path, required=True)
    parser.add_argument("--ascend-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pycache-root", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or not 0 < args.port < 65536 or args.device < 0:
        parser.error("Requires Linux and valid logical device/port")
    args.executable = args.executable.resolve(strict=True)
    args.model = args.model.resolve(strict=True)
    try:
        output = restore_runtime.private_path(args.output)
        args.pycache_root = restore_runtime.private_path(args.pycache_root)
        if output == args.model or args.model in output.parents:
            raise ValueError("Output overlaps the model")
        if args.pycache_root == args.model or args.model in args.pycache_root.parents:
            raise ValueError("Bytecode overlaps the model")
        manifest = restore_runtime.read_manifest()
        sources = {
            name: restore_runtime.verify_restored_source(
                path, manifest["sources"][name]
            )
            for name, path in (
                ("core", args.core_source),
                ("ascend", args.ascend_source),
            )
        }
        smoke_backend.ipc_endpoint(output / "replay.sock")
        with socket.socket() as check:
            check.bind(("127.0.0.1", args.port))
        output.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    report = {
        "schema_version": 1,
        "scope": "qwen_plain_text_identity_subset",
        "status": "fail",
        "sources": sources,
        "device": args.device,
        "checks": {},
        "hash_algorithm": "xxhash",
        "first_block_seed_validated": False,
        "router_execution_validated": False,
        "complete_recovery_validated": False,
        "performance_validated": False,
    }
    started = time.monotonic()
    try:
        run(args, output, report)
        report["status"] = "pass"
    except Exception as error:
        report["error"] = {"class": type(error).__name__, "message": str(error)}
    report["elapsed_seconds"] = time.monotonic() - started
    with (output / "summary.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(f"KV identity subset: {report['status']}; {output / 'summary.json'}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
