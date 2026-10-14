"""Explicitly fetch ten hash-pinned HUST files for the CPU source-method gate.

Never imports or installs HUST. The output must be fresh and project-private.
Downloads are bounded, TLS-verified and checked before writing source files.
"""

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from tools.hust_source_profiles import (
    CED_CAPTURE_HASHES,
    CED_COMMIT,
    CED_ENTRY_HASHES,
    CED_PROFILE,
    CED_SOURCE_PATHS,
)
from tools.restore_runtime import private_path

MAX_SOURCE_BYTES = 1024 * 1024


def fetch_sources(output, *, open_url=urllib.request.urlopen):
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "fail", "profile_id": CED_PROFILE, "files": {}, "attempts": {}}
    hashes = CED_CAPTURE_HASHES | CED_ENTRY_HASHES
    try:
        for name, relative in CED_SOURCE_PATHS.items():
            url = (
                "https://raw.githubusercontent.com/vLLM-HUST/vllm-hust/"
                f"{CED_COMMIT}/{relative}"
            )
            for attempt in range(1, 4):
                report["attempts"][name] = attempt
                try:
                    with open_url(url, timeout=20) as response:
                        payload = response.read(MAX_SOURCE_BYTES + 1)
                except OSError:
                    if attempt == 3:
                        raise
                else:
                    break
            if len(payload) > MAX_SOURCE_BYTES:
                raise ValueError(f"Source exceeds download budget: {name}")
            digest = hashlib.sha256(payload).hexdigest()
            if digest != hashes[name]:
                raise ValueError(f"Fixed source hash mismatch: {name}")
            with (output / name).open("xb") as handle:
                handle.write(payload)
            report["files"][name] = {"path": relative, "sha256": digest}
        report["status"] = "pass"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        with (output / "fetch.json").open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", required=True, action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        output = private_path(args.output)
        fetch_sources(output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"status": "pass", "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
