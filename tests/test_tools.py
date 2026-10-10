import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "suite", ["npu-smoke", "npu-routing", "performance", "release"]
)
def test_unimplemented_suite_never_passes(suite, tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/check.py"),
            "--suite",
            suite,
            "--output",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 3
    report = json.loads((tmp_path / f"{suite}.json").read_text("utf-8"))
    assert report["status"] == "not-run"
    assert report["commands"] == []
