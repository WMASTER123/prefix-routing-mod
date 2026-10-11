import io
import json
from unittest.mock import MagicMock

import pytest

from tools import regress_kv_identity as probe


def opener_with(payload):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value = io.BytesIO(payload)
    return opener


def test_post_rejects_oversized_and_error_responses():
    for data in (b"x" * (1024**2 + 1), b'{"error":"failed"}', b"[]"):
        with pytest.raises(ValueError):
            probe.post(opener_with(data), "http://127.0.0.1:1", "/tokenize", {})


def test_request_tokens_are_sent_but_not_logged_in_result():
    opener = opener_with(b'{"usage":{"prompt_tokens":2}}')
    result = probe.post(
        opener, "http://127.0.0.1:1", "/v1/completions", {"prompt": [123, 456]}
    )
    request = opener.open.call_args.args[0]
    assert json.loads(request.data)["prompt"] == [123, 456]
    assert result == {"usage": {"prompt_tokens": 2}}
    assert opener.open.call_args.kwargs["timeout"] == 120
