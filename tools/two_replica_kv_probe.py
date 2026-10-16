"""Explicit two-TP1 feasibility and online-directory isolation qualification.

Uses existing native launch, M7 decoder, original M2 and process cleanup.
This is not a capacity/strategy benchmark or production HTTP assembly.
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from prefix_routing_mod.adapters.kv import local_block_digest
from prefix_routing_mod.adapters.profiles import inspect_backend
from tools.hust_native_probe import (
    PROFILE,
    cached_tokens,
    command,
    require_terminal_plugins,
    service_environment,
)
from tools.live_terminal_cases import MODEL, request_json
from tools.online_kv_directory import IndexOwner, OnlineDirectory
from tools.regress_kv_recovery import OwnedBackend
from tools.restore_runtime import private_path


def run(args, output, report):
    report["terminal_entry_points"] = require_terminal_plugins()
    from transformers import AutoTokenizer

    report["interface"] = inspect_backend(PROFILE)
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
        raise ValueError("CANN ACL fingerprint differs")
    assets = {
        name: hashlib.sha256((args.model / name).read_bytes()).hexdigest()
        for name in (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "chat_template.jinja",
        )
        if (args.model / name).is_file()
    }
    report["model_assets_sha256"] = assets
    identity = hashlib.sha256(
        json.dumps(
            {
                "assets": assets,
                "profile": PROFILE,
                "layout": "fp16-full-attention-tp1-dp1-group0-block128-sha256",
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    prompts = [
        "A reusable prefix supports efficient inference. " * 64,
        "Distributed directories require independent replica identities. " * 64,
    ]
    token_ids = [tokenizer.encode(p, add_special_tokens=False) for p in prompts]
    if any(not 256 <= len(tokens) <= 900 for tokens in token_ids):
        raise ValueError("Input outside controlled model context")
    queries = [
        [
            local_block_digest(tokens[i : i + 128]).hex()
            for i in range(0, len(tokens) - 127, 128)
        ]
        for tokens in token_ids
    ]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    backends, directories = [], []
    index = None
    configs = []
    try:
        # Start both before attaching the control loops: startup of the other
        # engine must not consume a live control request's time budget.
        for device in (0, 1):
            root = output / f"replica-{device}"
            root.mkdir()
            token = secrets.token_hex(32)
            origin = f"http://127.0.0.1:{args.port + device}"
            env = service_environment(
                device,
                root,
                acl,
                pycache_root=args.pycache_root,
                token=token,
                worker=f"replica-{device}",
            )
            argv = command(
                Path(sys.executable).with_name("vllm"),
                args.model,
                args.port + device,
                root,
            )
            backend = OwnedBackend(argv, env, root / "backend", opener, origin)
            backends.append(backend)
            configs.append((root, token, origin))
            backend.start()
        report["both_replicas_running"] = all(
            b.process.poll() is None for b in backends
        )
        index = IndexOwner(args.index_binary)
        for backend, (root, token, origin) in zip(backends, configs, strict=True):
            directory = OnlineDirectory(
                index, backend, opener, origin, root, token, identity
            )
            directories.append(directory)
            directory.start()
            if directory.lookup(queries[0]) != 0:
                raise ValueError("Cold partition did not start at observed miss")

        def request(replica, prompt_index):
            reply = request_json(
                opener,
                configs[replica][2],
                "/v1/completions",
                body={
                    "model": MODEL,
                    "prompt": prompts[prompt_index],
                    "max_tokens": 16,
                    "temperature": 0,
                },
            )
            if (
                reply["usage"]["prompt_tokens"] != len(token_ids[prompt_index])
                or not reply["choices"]
            ):
                raise ValueError("Actual request tokens/output differ")
            return {
                "replica": replica,
                "prompt_tokens": reply["usage"]["prompt_tokens"],
                "cached_tokens": cached_tokens(reply["usage"]),
            }

        def wait_match(replica, query, expected):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                value = directories[replica].lookup(query)
                if value == expected:
                    return value
                time.sleep(0.05)
            raise ValueError("Live partition did not reach expected match")

        requests = [request(0, 0)]
        wait_match(0, queries[0], len(queries[0]))
        if directories[1].lookup(queries[0]) != 0:
            raise ValueError("Replica-a residency contaminated replica-b")
        report["one_sided_residency_isolated"] = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request, replica, replica) for replica in (0, 1)]
            requests.extend(future.result() for future in futures)
        for replica in (0, 1):
            wait_match(replica, queries[replica], len(queries[replica]))
        if requests[1]["cached_tokens"] is None or requests[1]["cached_tokens"] <= 0:
            raise ValueError("Repeat lacks actual backend cache reuse")
        directories[0].close()
        directories[0].check()
        after = index.call(
            {"kind": "lookup", "binding": directories[0].binding, "hashes": queries[0]}
        )
        if after["matched_blocks"] is not None:
            raise ValueError("Disconnected partition remains trusted")
        wait_match(1, queries[1], len(queries[1]))
        requests.append(request(1, 1))
        report.update(
            requests=requests,
            disconnect_isolated=True,
            shared_index_owner_pid=index.process.pid,
        )
        report["status"] = "pass"
    finally:
        errors = []
        report["directories"] = []
        for directory in reversed(directories):
            try:
                directory.close()
                directory.check()
            except Exception as error:
                errors.append(str(error))
            report["directories"].append(directory.summary())
        if index is not None:
            try:
                index.close()
            except Exception as error:
                errors.append(str(error))
        report["cleanup"] = []
        for backend in reversed(backends):
            try:
                backend.stop()
            except Exception as error:
                errors.append(str(error))
            report["cleanup"].append(backend.cleanup)
        if errors:
            raise RuntimeError(
                "Two-replica cleanup/source failure: " + "; ".join(errors)
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--index-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pycache-root", type=Path)
    parser.add_argument("--port", type=int, default=18170)
    args = parser.parse_args()
    if os.name != "posix" or not 0 < args.port < 65535:
        parser.error("Explicit Linux execution and two consecutive ports required")
    args.model = args.model.resolve(strict=True)
    args.index_binary = private_path(args.index_binary).resolve(strict=True)
    output = private_path(args.output)
    for port in (args.port, args.port + 1):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "fail",
        "scope": "two-TP1-online-directory-feasibility",
        "full_routing_verified": False,
        "performance_verified": False,
    }
    try:
        run(args, output, report)
    except Exception as error:
        report.update(status="fail", error=f"{type(error).__name__}: {error}")
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
