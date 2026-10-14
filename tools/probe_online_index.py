"""Explicit CPU checks across the real long-lived native index process."""

import argparse
import json
import time
from pathlib import Path

from tools.online_kv_directory import IndexOwner


def run(binary):
    index = IndexOwner(binary)
    cases = []

    def binding(worker):
        return {
            "worker": worker,
            "instance": 1,
            "publisher": "controlled-test",
            "identity": "a" * 64,
            "block_size": 128,
        }

    def send(b, kind, **fields):
        return index.call({"kind": kind, "binding": b, **fields})

    def lookup(b):
        return send(b, "lookup", hashes=["b" * 64])["matched_blocks"]

    def confirm(b, through):
        nonce = send(b, "begin_control")["nonce"]
        send(b, "confirm", nonce=nonce, through=through)

    def reject(b, kind, **fields):
        try:
            send(b, kind, **fields)
        except ValueError:
            return
        raise AssertionError("Expected index rejection")

    a, b = binding("a"), binding("b")
    try:
        send(a, "cold", lease_ms=1000)
        send(b, "cold", lease_ms=1)
        reject(binding("c"), "cold", lease_ms=1000)
        assert lookup(a) is None
        confirm(a, 0)
        assert lookup(a) == 0
        cases.append("two-partition-total-budget-and-cold-trust")
        send(
            a,
            "live",
            batch={
                "sequence": 0,
                "ops": [
                    {"kind": "store", "id": "x", "parent": None, "local": "b" * 64}
                ],
            },
        )
        assert lookup(a) == 1
        cases.append("live-event-updates-same-running-index")
        assert lookup({**a, "identity": "c" * 64}) is None
        cases.append("foreign-identity-is-unknown")
        confirm(b, 0)
        time.sleep(0.02)
        assert lookup(b) is None and lookup(a) == 1
        cases.append("lease-expiry-is-per-partition")
        reject(a, "live", batch={"sequence": 2, "ops": [{"kind": "clear"}]})
        assert lookup(a) is None
        reject(a, "begin_control")
        cases.append("gap-withdraws-trust-and-cannot-reconfirm")
    finally:
        index.close()
    return {"status": "pass", "scope": "CPU-native-process", "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.binary.resolve(strict=True))))


if __name__ == "__main__":
    main()
