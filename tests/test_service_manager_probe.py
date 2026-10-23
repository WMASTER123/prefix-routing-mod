import json

import pytest

from tools.probe_service_manager import read_rows


def test_pending_log_line_is_not_mistaken_for_bad_json(tmp_path):
    path = tmp_path / "fixture.jsonl"
    assert read_rows(path) == []
    path.write_text('{"pid": 1}\n{"shutdown":')
    assert read_rows(path) == [{"pid": 1}]
    path.write_text('{"pid": 1}\n{"shutdown": "Clean"}\n')
    assert read_rows(path) == [{"pid": 1}, {"shutdown": "Clean"}]


def test_malformed_complete_log_line_is_not_silently_discarded(tmp_path):
    path = tmp_path / "fixture.jsonl"
    path.write_text("not-json\n")
    with pytest.raises(json.JSONDecodeError):
        read_rows(path)
