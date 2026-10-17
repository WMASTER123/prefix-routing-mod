import threading

import pytest

from tools import npu_stability_probe as probe


def test_stability_uses_actual_bounded_completions_and_retains_only_counters(
    monkeypatch,
):
    now = [0.0]
    budgets = []
    reports = []

    def request(*args, body, **kwargs):
        budgets.append(body["max_tokens"])
        now[0] += 2
        return {
            "usage": {
                "completion_tokens": body["max_tokens"],
                "prompt_tokens_details": {"cached_tokens": 384},
            },
            "choices": [{"text": "not retained"}],
        }

    monkeypatch.setattr(probe, "request_json", request)
    result = probe.exercise(
        None,
        "http://127.0.0.1:1",
        0,
        5,
        reports.append,
        threading.Event(),
        clock=lambda: now[0],
    )
    assert budgets == [16, 64, 128]
    assert result["requests"] == 3
    assert result["generated_tokens"] == 208
    assert result["cached_tokens"] == 1152
    assert result["latency_seconds_max"] == 2
    assert reports == [result]
    assert "text" not in str(result)


def test_stability_stop_prevents_further_requests_and_invalid_output_fails(monkeypatch):
    def request(*args, **kwargs):
        return {"usage": {"completion_tokens": True}, "choices": [{}]}

    monkeypatch.setattr(probe, "request_json", request)
    with pytest.raises(ValueError, match="completion"):
        probe.exercise(
            None,
            "http://127.0.0.1:1",
            0,
            5,
            lambda _: None,
            threading.Event(),
            clock=lambda: 0,
        )
    stopped = threading.Event()
    stopped.set()
    result = probe.exercise(
        None, "http://127.0.0.1:1", 0, 5, lambda _: None, stopped, clock=lambda: 0
    )
    assert result["requests"] == 0
