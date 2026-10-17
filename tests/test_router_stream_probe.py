"""Guard the manual native probe without substituting for its Rust execution."""

import os
from pathlib import Path

import pytest

from tools.router_stream_probe import run


def test_stream_probe_requires_supported_process_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ValueError, match="Linux process groups"):
        run(tmp_path / "missing", tmp_path / "report")
    assert not (tmp_path / "report").exists()


@pytest.mark.skipif(os.name != "posix", reason="Linux native probe")
def test_stream_probe_preserves_existing_evidence(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "report.json"
    marker.write_text("previous evidence")
    with pytest.raises(FileExistsError):
        run(Path("/bin/true"), output)
    assert marker.read_text() == "previous evidence"


@pytest.mark.skipif(os.name != "posix", reason="Linux executable permissions")
def test_stream_probe_rejects_non_executable_before_creating_output(tmp_path):
    binary = tmp_path / "not-executable"
    binary.write_text("fixture")
    binary.chmod(0o600)
    with pytest.raises(ValueError, match="executable"):
        run(binary, tmp_path / "report")
    assert not (tmp_path / "report").exists()
