import json
from importlib.metadata import entry_points
from pathlib import Path

from vllm_hust_ext.manifest import activation_blocker, load_manifest

import prefix_routing_mod

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = "io.github.wmaster123.prefix-routing-mod"


def test_descriptor_is_discoverable_but_not_activatable():
    path = Path(prefix_routing_mod.__file__).with_name("vllm-hust-extension-v0.3.json")
    manifest = load_manifest(path)
    assert manifest.bundle_id == BUNDLE
    assert manifest.schema_version == "0.3"
    assert "descriptor-only" in activation_blocker(manifest)
    entries = entry_points(group="vllm_hust.extension_bundles", name=BUNDLE)
    assert len(entries) == 1
    assert next(iter(entries)).load() is prefix_routing_mod


def test_metadata_is_honest_and_activation_empty():
    metadata = json.loads((ROOT / "MOD_METADATA.json").read_text("utf-8"))
    manifest = json.loads(
        Path(prefix_routing_mod.__file__)
        .with_name("vllm-hust-extension-v0.3.json")
        .read_text("utf-8")
    )
    assert metadata["mod_id"] == manifest["extension_id"] == BUNDLE
    assert metadata["canonical_repository"] == (
        "https://github.com/WMASTER123/prefix-routing-mod"
    )
    assert not metadata["lifecycle"]["default_enabled"]
    assert metadata["evidence"]["performance_claims"] == []
    assert manifest["activation"] == {
        "entry_points": [],
        "environment": {},
        "additional_config": {},
    }
    assert manifest["components"] == []
