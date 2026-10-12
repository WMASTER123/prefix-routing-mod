"""Guard/tool checks; fixed-source execution is an explicit manual CPU gate."""

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import audit_backend_terminal as audit


def test_missing_source_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit.load_sources(tmp_path)


def test_changed_source_rejected_before_execution(tmp_path):
    (tmp_path / "scheduler.py").write_text("raise RuntimeError('must not run')")
    with pytest.raises(ValueError, match="hash mismatch"):
        audit.load_sources(tmp_path)


def test_all_sources_checked_before_compilation(tmp_path, monkeypatch):
    payload = b"raise RuntimeError('must not run')\n"
    (tmp_path / "scheduler.py").write_bytes(payload)
    monkeypatch.setattr(
        audit,
        "SOURCE_HASHES",
        {
            "scheduler.py": hashlib.sha256(payload).hexdigest(),
            "missing.py": "0" * 64,
        },
    )
    with pytest.raises(FileNotFoundError):
        audit.load_sources(tmp_path)


def test_method_extraction_preserves_body_and_postpones_annotations():
    sources = {
        "fixture.py": ast.parse(
            "raise RuntimeError('module must not run')\n"
            "class Fixture:\n"
            "    def selected(self, value: Unknown) -> Other:\n"
            "        return value + 7\n"
            "    def unselected(self):\n"
            "        raise RuntimeError('not selected')\n"
        )
    }
    cls = audit.source_class(sources, "fixture.py", "Fixture", ("selected",), {})
    assert cls().selected(5) == 12
    assert not hasattr(cls, "unselected")


@pytest.mark.parametrize(
    "source,match",
    [
        ("class Other: pass", "class missing"),
        ("class Fixture: pass\nclass Fixture: pass", "ambiguous"),
        ("class Fixture: pass", "method set"),
        ("class Fixture:\n @staticmethod\n def selected(): pass", "decorated"),
    ],
)
def test_extraction_rejects_changed_shape(source, match):
    with pytest.raises(ValueError, match=match):
        audit.source_class({"f": ast.parse(source)}, "f", "Fixture", ("selected",), {})


def test_existing_evidence_not_overwritten(tmp_path):
    target = tmp_path / "report.json"
    target.write_text("prior evidence")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.audit_backend_terminal",
            "--source-directory",
            str(tmp_path),
            "--output",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert target.read_text() == "prior evidence"


def test_main_records_failure_honestly(tmp_path, monkeypatch, capsys):
    target = tmp_path / "out.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit", "--source-directory", str(tmp_path), "--output", str(target)],
    )
    monkeypatch.setattr(audit, "run_audit", lambda directory: {"status": "fail"})
    assert audit.main() == 1
    assert json.loads(target.read_text()) == {"status": "fail"}
    assert '"fail"' in capsys.readouterr().out


def test_hash_manifest_fixed():
    assert len(audit.SOURCE_HASHES) == 4
    assert all(len(value) == 64 for value in audit.SOURCE_HASHES.values())
    assert len(audit.CORE_COMMIT) == 40
    # The tool is not installed as an engine plugin or runtime import.
    root = Path(__file__).resolve().parents[1]
    assert "audit_backend_terminal" not in (root / "pyproject.toml").read_text()
