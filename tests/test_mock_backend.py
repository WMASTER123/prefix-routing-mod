import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.fixtures.mock_backend import mock_backend

pytestmark = pytest.mark.integration


def test_health_and_text_response():
    with mock_backend() as origin:
        with urlopen(origin + "/health", timeout=5) as response:
            assert response.status == 200
        request = Request(
            origin + "/v1/chat/completions",
            data=json.dumps({"model": "fixture", "stream": False}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["choices"][0]["message"]["content"] == (
                "hello world"
            )


def test_sse_framing_and_done():
    with mock_backend() as origin:
        request = Request(
            origin + "/v1/chat/completions",
            data=b'{"model":"fixture","stream":true}',
        )
        with urlopen(request, timeout=5) as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            lines = [line.decode().strip() for line in response if line.strip()]
            assert len(lines) == 3
            assert lines[-1] == "data: [DONE]"


def test_error_is_distinguishable():
    with mock_backend() as origin:
        request = Request(
            origin + "/v1/chat/completions", data=b'{"model":"fixture-error"}'
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 503
        error.value.close()
