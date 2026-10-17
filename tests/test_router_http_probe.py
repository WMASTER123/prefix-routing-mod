from urllib.request import ProxyHandler, build_opener

import pytest

from tests.fixtures.mock_backend import Handler, mock_backend
from tools.router_http_probe import MAX_RESPONSE_BYTES, exchange


def test_probe_preserves_backend_error_response():
    with mock_backend() as origin:
        status, _, body = exchange(
            build_opener(ProxyHandler({})),
            origin + "/v1/completions",
            {"model": "fixture-error"},
        )
        assert status == 503
        assert b"synthetic unavailable" in body


def test_probe_bounds_backend_response_memory():
    class OversizedHandler(Handler):
        def do_POST(self):
            self.close_connection = True
            self.reply(200, b"x" * (MAX_RESPONSE_BYTES + 1))

    with (
        mock_backend(OversizedHandler) as origin,
        pytest.raises(ValueError, match="exceeded budget"),
    ):
        exchange(build_opener(ProxyHandler({})), origin + "/v1/completions", {})
