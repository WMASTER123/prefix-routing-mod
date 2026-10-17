import json
import os
import urllib.error

import pytest

from tools import live_terminal_cases as cases


def test_actual_probe_multi_sample_request_uses_non_greedy_sampling(
    tmp_path, monkeypatch
):
    class InspectedRequest(Exception):
        pass

    def request(opener, origin, route, *, body=None, headers=None):
        if headers is None:
            raise urllib.error.HTTPError(
                "http://fixture", 403, "unauthorized", {}, None
            )
        if route == "/prefix-routing/terminal":
            return {
                "binding": {
                    "worker_id": "worker",
                    "instance_epoch": "instance",
                    "observer_epoch": "observer",
                },
                "boundary": 0,
                "records": [],
                "core_pid": os.getpid() + 2,
                "frontend_pid": os.getpid() + 1,
            }
        assert route == "/v1/completions" and body["n"] > 1
        assert body["temperature"] > 0  # HUST rejects n>1 greedy before submission.
        raise InspectedRequest("request inspected before any real network")

    monkeypatch.setattr(cases, "request_json", request)
    with pytest.raises(InspectedRequest):
        cases.run(None, "fixture", tmp_path, "credential-not-for-logs")
    report = json.loads((tmp_path / "terminal-report.json").read_text())
    assert report["status"] == "fail" and report["control_auth_rejected"]
    assert (
        "credential-not-for-logs" not in (tmp_path / "terminal-report.json").read_text()
    )


def test_backend_validation_failure_keeps_bounded_diagnostic():
    import io

    class Opener:
        def open(self, *args, **kwargs):
            payload = json.dumps(
                {"error": {"message": "n must be 1 when using greedy sampling"}}
            ).encode()
            raise urllib.error.HTTPError(
                "http://fixture", 400, "bad request", {}, io.BytesIO(payload)
            )

    with pytest.raises(RuntimeError, match="greedy sampling"):
        cases.request_json(Opener(), "http://fixture", "/v1/completions", body={})
