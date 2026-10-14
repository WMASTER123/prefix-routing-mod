"""Compare fixed-Router completion tokenization with the fixed HUST asset.

This is a Host-side compatibility probe. It loads tokenizer libraries but does
not start vLLM, a model service, or an NPU process.
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from prefix_routing_mod.adapters.kv import local_block_digest

MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def run(binary: Path, model: Path, output: Path) -> dict:
    if os.name != "posix":
        raise ValueError("fixed Router tokenizer probe requires its Linux binary")
    binary = binary.resolve(strict=True)
    model = model.resolve(strict=True)
    tokenizer_path = model / "tokenizer.json"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("probe binary must be executable")
    if not tokenizer_path.is_file():
        raise ValueError("model tokenizer.json is missing")
    output.mkdir(parents=True, exist_ok=False)
    with binary.open("rb") as stream:
        binary_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    with tokenizer_path.open("rb") as stream:
        tokenizer_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        "status": "fail",
        "binary_sha256": binary_sha,
        "tokenizer_sha256": tokenizer_sha,
    }
    try:
        completed = subprocess.run(
            [str(binary), str(tokenizer_path), tokenizer_sha],
            check=False,
            capture_output=True,
            timeout=30,
        )
        combined = completed.stdout + completed.stderr
        if len(combined) > MAX_OUTPUT_BYTES:
            raise ValueError("probe output exceeded evidence budget")
        (output / "probe.log").write_bytes(combined)
        if completed.returncode:
            raise RuntimeError(f"probe exited {completed.returncode}")
        records = [
            json.loads(line)
            for line in completed.stdout.decode().splitlines()
            if line.startswith('{"status":')
        ]
        if len(records) != 1 or records[0].get("status") != "pass":
            raise ValueError("missing bounded probe result")
        record = records[0]

        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
        expected_tokens = tokenizer.encode(record["prompt"], add_special_tokens=False)
        expected_hashes = [
            local_block_digest(expected_tokens[offset : offset + 128]).hex()
            for offset in range(0, len(expected_tokens) - 127, 128)
        ]
        if record["token_ids"] != expected_tokens:
            raise ValueError("fixed Router and HUST tokenizer tokens differ")
        if record["block_hashes"] != expected_hashes:
            raise ValueError("completion feature hashes differ from M2 query keys")
        report.update(
            {
                "status": "pass",
                "token_count": len(expected_tokens),
                "complete_block_count": len(expected_hashes),
                "identity": record["identity"],
                "checks": [
                    "fixed_asset_digest",
                    "router_hust_token_ids_equal",
                    "m1_m2_complete_block_hashes_equal",
                ],
            }
        )
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.binary, args.model, args.output)
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
