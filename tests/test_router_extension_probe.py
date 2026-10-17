import pytest

from tools.router_extension_probe import MAX_LOG_BYTES, prototype_result


def test_prototype_report_preserves_single_result(tmp_path):
    log = tmp_path / "probe.log"
    log.write_text('diagnostic\n{"status":"pass","sent":1}\n')
    assert prototype_result(log) == {"status": "pass", "sent": 1}


@pytest.mark.parametrize("text", ["", '{"status":"fail"}\n', '{"status":"pass"}\n' * 2])
def test_prototype_report_rejects_missing_failed_or_duplicate_result(tmp_path, text):
    log = tmp_path / "probe.log"
    log.write_text(text)
    with pytest.raises(ValueError, match="missing bounded"):
        prototype_result(log)


def test_prototype_report_has_memory_budget(tmp_path):
    log = tmp_path / "probe.log"
    log.write_bytes(b"x" * (MAX_LOG_BYTES + 1))
    with pytest.raises(ValueError, match="evidence budget"):
        prototype_result(log)
