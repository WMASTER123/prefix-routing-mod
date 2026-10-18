"""Check that an experimental dependency addition preserves the pinned host lock."""

import argparse
import json
import tomllib
from pathlib import Path


def audit(baseline: Path, candidate: Path) -> dict:
    def packages(path):
        with path.open("rb") as stream:
            lock = tomllib.load(stream)
        return {(p["name"], p["version"], p.get("source")): p for p in lock["package"]}

    original, current = packages(baseline), packages(candidate)
    changed = []
    extended = []
    for key, value in original.items():
        replacement = current.get(key)
        if replacement is None or {
            k: v for k, v in value.items() if k != "dependencies"
        } != {k: v for k, v in replacement.items() if k != "dependencies"}:
            changed.append(list(key))
            continue
        old_dependencies = set(value.get("dependencies", []))
        new_dependencies = set(replacement.get("dependencies", []))
        if not old_dependencies <= new_dependencies:
            changed.append(list(key))
        elif old_dependencies != new_dependencies:
            extended.append(
                {
                    "package": list(key),
                    "added": sorted(new_dependencies - old_dependencies),
                }
            )
    added = sorted(k for k in current if k not in original)
    return {
        "status": "pass" if not changed else "fail",
        "changed_or_removed_existing_packages": changed,
        "added_packages": [list(k) for k in added],
        "extended_existing_dependency_lists": extended,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    report = audit(args.baseline, args.candidate)
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
