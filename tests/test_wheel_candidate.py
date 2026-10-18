import hashlib
import json
from pathlib import Path

import pytest

from tools import hust_native_probe
from tools import restore_wheel_runtime as restore


def test_candidate_lock_hashes_and_reused_wheel_inputs_are_complete():
    root = Path(__file__).resolve().parents[1] / "runtime/candidates"
    data = json.loads((root / "hust-cedb477.json").read_text("utf-8"))
    for name, digest in data["locks"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    assert len(data["wheels"]) == 5
    assert any(name.startswith("arctic_") for name in data["wheels"])


def test_input_drift_fails_before_install(tmp_path, monkeypatch):
    manifest = {
        "locks": {"runtime.txt": hashlib.sha256(b"lock").hexdigest()},
        "wheels": {"engine.whl": hashlib.sha256(b"wheel").hexdigest()},
    }
    (tmp_path / "hust-cedb477.json").write_text(json.dumps(manifest), "utf-8")
    (tmp_path / "runtime.txt").write_bytes(b"lock")
    (tmp_path / "engine.whl").write_bytes(b"wheel")
    monkeypatch.setattr(restore, "CANDIDATES", tmp_path)
    assert restore.load_inputs(tmp_path) == manifest
    (tmp_path / "engine.whl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="fingerprint"):
        restore.load_inputs(tmp_path)


def test_install_order_has_ascend_overlay_after_both_dependency_locks(tmp_path):
    manifest = json.loads((restore.CANDIDATES / "hust-cedb477.json").read_text("utf-8"))
    steps = restore.installation_steps(
        Path("uv"),
        Path("python"),
        tmp_path,
        tmp_path / "wheels",
        manifest,
        offline=True,
    )
    assert [name for name, _ in steps] == [
        "venv",
        "dependencies",
        "extra",
        "triton-overlay",
        "engines",
        "dependencies-check",
        "overlay-check",
    ]
    assert "--require-hashes" in steps[1][1] and "--require-hashes" in steps[2][1]
    assert "--reinstall" in steps[3][1] and "--no-deps" in steps[4][1]
    assert "--offline" in steps[1][1]


def test_native_probe_uses_explicit_single_replica_and_new_config(tmp_path):
    short = Path(tmp_path.anchor) / "probe"
    argv = hust_native_probe.command(Path("vllm"), tmp_path / "model", 18146, short)
    assert argv[argv.index("--tensor-parallel-size") + 1] == "1"
    assert argv[argv.index("--dtype") + 1] == "float16"
    assert argv[argv.index("--block-size") + 1] == "128"
    assert "--enable-prefix-routing" not in argv
    assert "--enforce-eager" in argv
    assert "--enable-prompt-tokens-details" in argv
    config = json.loads(argv[argv.index("--kv-events-config") + 1])
    assert config["buffer_steps"] == 16 and config["replay_endpoint"].startswith(
        "ipc://"
    )


def test_native_probe_rejects_existing_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(hust_native_probe, "private_path", lambda path: path)
    with pytest.raises(SystemExit):
        hust_native_probe.main(
            [
                "--execute",
                "--model",
                str(tmp_path),
                "--output",
                str(tmp_path),
                "--device",
                "0",
            ]
        )


def test_native_probe_rejects_wrong_router_protocol_before_backend(
    tmp_path, monkeypatch, capsys
):
    model = tmp_path / "model"
    model.mkdir()
    wrong = tmp_path / "wrong-router-example"
    wrong.write_bytes(b"not the live terminal stage driver")
    monkeypatch.setattr(hust_native_probe, "private_path", lambda path: path)
    monkeypatch.setattr(
        hust_native_probe,
        "run",
        lambda *args: pytest.fail("wrong binary must fail before backend launch"),
    )
    with pytest.raises(SystemExit):
        hust_native_probe.main(
            [
                "--execute",
                "--model",
                str(model),
                "--output",
                str(tmp_path / "output"),
                "--device",
                "0",
                "--terminal-observer",
                "--router-binary",
                str(wrong),
            ]
        )
    assert "stage-driver protocol" in capsys.readouterr().err


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": None}])
def test_absent_cache_details_remain_unknown(details):
    assert hust_native_probe.cached_tokens({"prompt_tokens_details": details}) is None


@pytest.mark.parametrize("value", [False, -1, "128"])
def test_invalid_cache_details_cannot_be_promoted_to_observed(value):
    with pytest.raises(ValueError):
        hust_native_probe.cached_tokens(
            {"prompt_tokens_details": {"cached_tokens": value}}
        )
