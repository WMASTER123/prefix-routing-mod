"""Cheap scope guard; actual native compilation is a separate manual gate."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_router_prototype_patch_touches_only_three_declared_host_files():
    patch = (ROOT / "experiments/router-extension/router-91b65ee.patch").read_text()
    changed = re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE)
    expected = {
        "src/policies/registry.rs",
        "src/routers/http/mod.rs",
        "src/routers/http/router.rs",
    }
    assert len(changed) == len(expected)
    assert {after for before, after in changed if before == after} == expected
    assert (
        len(re.findall(r"^\+\s+guard\.transport_failed\(\);$", patch, re.MULTILINE))
        == 3
    )


def test_index_probe_direct_dependencies_remain_fixed():
    manifest = (ROOT / "experiments/router-index/Cargo.toml").read_text()
    assert 'dashmap = "=6.2.1"' in manifest
    assert 'parking_lot = "=0.12.5"' in manifest
    lock = (ROOT / "experiments/router-index/Cargo.lock").read_text()
    assert 'name = "prefix-router-index-probe"' in lock
