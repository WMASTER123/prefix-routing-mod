from pathlib import Path

import pytest

from prefix_routing_mod import manager


def test_manager_entry_requires_absolute_existing_router_and_bounded_grace(tmp_path):
    router = tmp_path / "router"
    router.write_text("fixture", encoding="utf-8")
    with pytest.raises(manager.ManagerEntryError, match="absolute"):
        manager.validate_manager_run(["router"], 30, 35)
    with pytest.raises(manager.ManagerEntryError, match="drain"):
        manager.validate_manager_run([str(router.resolve())], 0, 35)
    with pytest.raises(manager.ManagerEntryError, match="grace"):
        manager.validate_manager_run([str(router.resolve())], 30, 30)
    assert manager.validate_manager_run(
        ["--", str(router.resolve()), "--config", "/private/config.json"], 30, 35
    ) == [str(router.resolve()), "--config", "/private/config.json"]


def test_manager_entry_reuses_pinned_supervisor_without_mutating_command(
    tmp_path, monkeypatch
):
    router = tmp_path / "router"
    router.write_text("fixture", encoding="utf-8")
    captured = {}

    class Distribution:
        @staticmethod
        def read_text(name):
            assert name == "direct_url.json"
            return '{"vcs_info":{"commit_id":"' + manager.MANAGER_COMMIT + '"}}'

    def supervise(command, *, env, shutdown_grace_seconds):
        captured.update(
            command=command,
            env=env,
            shutdown_grace_seconds=shutdown_grace_seconds,
        )
        return 17

    monkeypatch.setattr(
        manager.importlib.metadata, "distribution", lambda _: Distribution()
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "vllm_hust_ext.process_supervisor",
        type("Supervisor", (), {"supervise": staticmethod(supervise)}),
    )
    command = [str(router.resolve()), "--config", "/private/config.json"]
    assert manager.run_router(command, 12.5, 15) == 17
    assert captured["command"] == command
    assert captured["shutdown_grace_seconds"] == 15
    assert captured["env"]["PREFIX_ROUTING_DRAIN_SECONDS"] == "12.5"


def test_manager_entry_rejects_an_unreviewed_manager(tmp_path, monkeypatch):
    router = Path(tmp_path / "router")
    router.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(manager, "_installed_manager_commit", lambda: "other")
    with pytest.raises(manager.ManagerEntryError, match="reviewed pinned commit"):
        manager.run_router([str(router.resolve())], 10, 12)
