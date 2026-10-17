from types import SimpleNamespace

import pytest

from tools import hust_runtime_chain


def test_stale_wheel_is_rejected_before_any_backend_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", lambda: {})
    monkeypatch.setattr(hust_runtime_chain, "FrontendEnds", object)

    def unexpected(*args, **kwargs):
        pytest.fail("A stale wheel must fail before backend inspection or launch")

    monkeypatch.setattr(hust_runtime_chain, "inspect_backend", unexpected)
    monkeypatch.setattr(hust_runtime_chain, "OwnedBackend", unexpected)
    with pytest.raises(RuntimeError, match="independent identity"):
        hust_runtime_chain.run(SimpleNamespace(), tmp_path, {})
