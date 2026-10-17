from tools.audit_router_candidate_lock import audit


def lock(tmp_path, name, packages):
    path = tmp_path / name
    path.write_text("version = 4\n" + packages, encoding="utf-8")
    return path


def package(version="1.0.0", dependencies='"old"', checksum="same"):
    return (
        '[[package]]\nname = "existing"\n'
        f'version = "{version}"\nchecksum = "{checksum}"\n'
        f"dependencies = [{dependencies}]\n"
    )


def test_new_dependency_can_extend_features_without_upgrading_host(tmp_path):
    old = lock(tmp_path, "old.lock", package())
    new = lock(tmp_path, "new.lock", package(dependencies='"old", "added"'))
    report = audit(old, new)
    assert report["status"] == "pass"
    assert report["extended_existing_dependency_lists"][0]["added"] == ["added"]


def test_host_version_checksum_or_dependency_removal_is_rejected(tmp_path):
    old = lock(tmp_path, "old.lock", package())
    for contents in (
        package(version="1.0.1"),
        package(checksum="changed"),
        package(dependencies=""),
    ):
        new = lock(tmp_path, "new.lock", contents)
        assert audit(old, new)["status"] == "fail"
