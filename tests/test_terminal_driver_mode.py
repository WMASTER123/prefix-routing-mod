"""Reject unsupported real-driver combinations before starting any backend."""

from types import SimpleNamespace

import pytest

from tools import hust_runtime_chain


@pytest.mark.parametrize(
    "change",
    [
        {"durable_store": False},
        {"managed_terminal_retirement": False},
        {"inject_retire_response_loss": True},
        {"exercise_router_recovery": True},
        {"exercise_control_gate": True},
        {"churn_families": 520},
    ],
)
def test_driver_mode_rejects_unqualified_combinations_before_launch(
    monkeypatch, tmp_path, change
):
    def unexpected(*args, **kwargs):
        pytest.fail("Rejected mode must not inspect or start backend")

    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", unexpected)
    monkeypatch.setattr(hust_runtime_chain, "OwnedBackend", unexpected)
    arguments = dict(
        use_terminal_driver=True,
        durable_store=True,
        managed_terminal_retirement=True,
        inject_retire_response_loss=False,
        exercise_router_recovery=False,
        exercise_control_gate=False,
        churn_families=0,
    )
    arguments.update(change)
    with pytest.raises(ValueError, match="requires|requires isolated"):
        hust_runtime_chain.run(SimpleNamespace(**arguments), tmp_path, {})


def test_background_mode_requires_driver_before_backend_inspection(
    monkeypatch, tmp_path
):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid background mode must not start or inspect the backend")

    monkeypatch.setattr(hust_runtime_chain, "require_terminal_plugins", unexpected)
    arguments = SimpleNamespace(
        background_terminal_driver=True, use_terminal_driver=False
    )
    with pytest.raises(ValueError, match="Background driver requires"):
        hust_runtime_chain.run(arguments, tmp_path, {})
