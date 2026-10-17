import hashlib
import re
from pathlib import Path


def test_hust_dependency_snapshot_is_complete_and_unchanged():
    root = Path(__file__).resolve().parents[1]
    text = (
        root / "runtime/candidates/hust-legacy-20260831-py312-aarch64.txt"
    ).read_text("utf-8")
    assert hashlib.sha256(text.encode()).hexdigest() == (
        "7bd3f97e9a632833a55209a285b2f3e2ff855e9ec1ce2136465a7de5f008d587"
    )
    pins = dict(re.findall(r"^([\w.-]+)==([^\s]+)", text, flags=re.MULTILINE))
    assert len(pins) == 186
    assert pins["numpy"] == "1.26.4"
    assert pins["torch-npu"] == "2.10.0"
    assert pins["triton-ascend"] == "3.2.1"
    assert pins["triton"] == "3.5.0"  # Declared by the Ascend distribution.
    assert "vllm" not in pins and "vllm-ascend" not in pins
    assert "vllm-ascend-hust" not in pins  # Local source builds are separate.
