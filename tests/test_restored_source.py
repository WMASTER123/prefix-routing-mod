from pathlib import Path

import pytest

from tools import restore_runtime


def source_tree(tmp_path, monkeypatch):
    restored = tmp_path / "artifacts" / "restored"
    original = tmp_path / "artifacts" / "original"
    restored.mkdir(parents=True)
    original.mkdir()
    identity = {"commit": "fixed", "origin": "https://github.com/owner/core.git"}
    origins = {restored: str(original), original: identity["origin"]}

    def git_value(path, *args):
        values = {
            ("rev-parse", "HEAD"): "fixed",
            ("rev-parse", "--is-shallow-repository"): "false",
            ("status", "--porcelain", "--untracked-files=all"): "",
        }
        if args == ("remote", "get-url", "origin"):
            return origins[Path(path)]
        return values[args]

    monkeypatch.setattr(restore_runtime, "git_value", git_value)
    return restored, original, origins, identity, git_value


def test_private_local_clone_keeps_auditable_canonical_chain(tmp_path, monkeypatch):
    restored, original, _, identity, _ = source_tree(tmp_path, monkeypatch)
    result = restore_runtime.verify_restored_source(restored, identity, root=tmp_path)
    assert result["canonical_origin"] == identity["origin"]
    assert result["origin_chain"] == [
        {"path": str(restored), "origin": str(original)},
        {"path": str(original), "origin": identity["origin"]},
    ]


@pytest.mark.parametrize("origin", ["https://github.com/other/core.git", "../other"])
def test_unrecognized_origin_rejected(tmp_path, monkeypatch, origin):
    restored, original, origins, identity, _ = source_tree(tmp_path, monkeypatch)
    origins[original] = origin
    with pytest.raises(ValueError):
        restore_runtime.verify_restored_source(restored, identity, root=tmp_path)


def test_external_local_source_and_cycles_rejected(tmp_path, monkeypatch):
    restored, original, origins, identity, _ = source_tree(tmp_path, monkeypatch)
    origins[original] = str(tmp_path.parent)
    with pytest.raises(ValueError, match="artifacts"):
        restore_runtime.verify_restored_source(restored, identity, root=tmp_path)
    origins[original] = str(restored)
    with pytest.raises(ValueError, match="cycle"):
        restore_runtime.verify_restored_source(restored, identity, root=tmp_path)


@pytest.mark.parametrize(
    "args,value",
    [
        (("rev-parse", "HEAD"), "different"),
        (("rev-parse", "--is-shallow-repository"), "true"),
        (("status", "--porcelain", "--untracked-files=all"), " M source.py"),
    ],
)
def test_every_chain_link_checks_content_and_history(
    tmp_path, monkeypatch, args, value
):
    restored, original, _, identity, git_value = source_tree(tmp_path, monkeypatch)

    def altered(path, *query):
        return value if path == original and query == args else git_value(path, *query)

    monkeypatch.setattr(restore_runtime, "git_value", altered)
    with pytest.raises(ValueError):
        restore_runtime.verify_restored_source(restored, identity, root=tmp_path)
