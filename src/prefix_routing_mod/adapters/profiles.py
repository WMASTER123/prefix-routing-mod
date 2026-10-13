"""Version mappings and explicit installed-interface checks, without engine imports.

Source fingerprints guard audited interfaces. Wheel/source provenance, runtime
configuration, instance identity and NPU qualification are separate checks.
"""

import hashlib
import importlib.metadata as metadata
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class BackendProfile:
    profile_id: str
    core_commit: str
    ascend_commit: str
    packages: tuple[tuple[str, str], ...]
    source_hashes: tuple[tuple[str, str], ...]
    wire_protocol: str
    native_replay: str
    cache_spec_kinds: tuple[str | None, ...]
    extra_event_fields: tuple[str, ...]
    hash_bytes: int | None
    block_size: int | None
    native_epoch: bool
    native_token_snapshot: bool
    publisher_hook_available: bool
    terminal_observer_available: bool


@lru_cache(maxsize=8)
def get_profile(profile_id: str) -> BackendProfile:
    data = json.loads(files(__package__).joinpath("profiles.json").read_text("utf-8"))
    if data["schema_version"] != 1:
        raise ValueError("Unsupported backend profile schema")
    try:
        raw = data["profiles"][profile_id]
    except KeyError as exc:
        raise ValueError("Unknown backend profile") from exc
    return BackendProfile(
        profile_id=profile_id,
        **{
            key: tuple(value.items())
            if key in ("packages", "source_hashes")
            else tuple(value)
            if key in ("cache_spec_kinds", "extra_event_fields")
            else value
            for key, value in raw.items()
        },
    )


def inspect_backend(profile_id: str, *, distribution=None) -> dict:
    """Check installed package versions and interface bytes; never activate hooks."""
    distribution = metadata.distribution if distribution is None else distribution
    profile = get_profile(profile_id)
    installed = {}
    for name, expected in profile.packages:
        try:
            dist = distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"Backend package is missing: {name}") from exc
        if dist.version != expected:
            raise RuntimeError(f"Unqualified backend package: {name} {dist.version}")
        installed[name] = dist
    ascend_name = next(name for name, _ in profile.packages if name != "vllm")
    other = "vllm-ascend" if ascend_name == "vllm-ascend-hust" else "vllm-ascend-hust"
    try:
        distribution(other)
    except metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError("Conflicting Ascend distributions share vllm_ascend")
    for relative, expected in profile.source_hashes:
        part = PurePosixPath(relative)
        if (
            part.is_absolute()
            or ".." in part.parts
            or part.parts[0]
            not in (
                "vllm",
                "vllm_ascend",
            )
        ):
            raise ValueError("Invalid profile interface path")
        dist = installed["vllm" if part.parts[0] == "vllm" else ascend_name]
        root = Path(dist.locate_file(part.parts[0])).resolve(strict=True)
        path = Path(dist.locate_file(relative)).resolve(strict=True)
        if not path.is_relative_to(root):
            raise RuntimeError("Backend interface escapes installed module")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Backend interface fingerprint differs: {relative}")
    return {
        "profile_id": profile_id,
        "interface_verified": True,
        "packages": dict(profile.packages),
        "core_commit": profile.core_commit,
        "ascend_commit": profile.ascend_commit,
        "native_replay": profile.native_replay,
        "native_epoch": profile.native_epoch,
        "native_token_snapshot": profile.native_token_snapshot,
        "publisher_hook_available": profile.publisher_hook_available,
        "terminal_observer_available": profile.terminal_observer_available,
        "runtime_verified": False,
        "full_recovery_verified": False,
        "routing_ready": False,
    }
