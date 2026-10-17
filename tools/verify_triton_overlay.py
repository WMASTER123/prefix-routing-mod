"""Check shared Triton files against Ascend RECORD, without importing engines."""

import argparse
import base64
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path


def record_entries(dist) -> dict:
    return {
        str(path): path
        for path in dist.files or []
        if str(path).startswith("triton/") and path.hash is not None
    }


def verify_files(generic, ascend) -> dict:
    expected = record_entries(ascend)
    shared = sorted(record_entries(generic).keys() & expected.keys())
    failures = []
    for name, entry in expected.items():
        path = Path(ascend.locate_file(entry))
        if not path.is_file():
            failures.append({"file": name, "reason": "missing"})
            continue
        digest = hashlib.new(entry.hash.mode, path.read_bytes()).digest()
        actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if actual != entry.hash.value:
            failures.append({"file": name, "reason": "hash_mismatch"})
    return {
        "status": "pass" if expected and shared and not failures else "fail",
        "ascend_record_files_checked": len(expected),
        "shared_files_checked": len(shared),
        "failures": failures,
        "generic_distribution": generic.version,
        "ascend_distribution": ascend.version,
        "runtime_backend_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    report = verify_files(
        metadata.distribution("triton"), metadata.distribution("triton-ascend")
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(f"Triton Ascend RECORD: {report['status']}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
