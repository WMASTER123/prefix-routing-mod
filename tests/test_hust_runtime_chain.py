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


def test_managed_retirement_requires_durable_store_before_backend_launch(
    monkeypatch, tmp_path
):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid managed retirement mode must fail before backend launch")

    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", unexpected)
    monkeypatch.setattr(hust_runtime_chain, "OwnedBackend", unexpected)
    args = SimpleNamespace(managed_terminal_retirement=True, durable_store=False)
    with pytest.raises(ValueError, match="requires --durable-store"):
        hust_runtime_chain.run(args, tmp_path, {})
