"""Narrow user entry for the reviewed extension-manager supervisor.

This module manages one explicitly named Router executable. It never starts,
stops, probes or adopts HUST backend processes and does not make the MOD
manifest activatable.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
from typing import NoReturn

MANAGER_COMMIT = "00d1b940dc6ce8d60e2cd28708b80d80e9c33a09"


class ManagerEntryError(ValueError):
    """Safe, user-facing Manager entry validation failure."""


def _installed_manager_commit() -> str | None:
    distribution = importlib.metadata.distribution("vllm-hust-ext")
    source = json.loads(distribution.read_text("direct_url.json") or "{}")
    value = source.get("vcs_info", {}).get("commit_id")
    return value if isinstance(value, str) else None


def validate_manager_run(
    command: list[str], drain_seconds: float, shutdown_grace_seconds: float
) -> list[str]:
    if not 0 < drain_seconds <= 300:
        raise ManagerEntryError("drain seconds must be in (0, 300]")
    if not drain_seconds + 1 <= shutdown_grace_seconds <= 360:
        raise ManagerEntryError(
            "shutdown grace must include the drain budget plus at least one second"
        )
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ManagerEntryError("an explicit Router command is required after --")
    executable = Path(command[0])
    if not executable.is_absolute():
        raise ManagerEntryError("the Router executable path must be absolute")
    try:
        executable = executable.resolve(strict=True)
    except OSError as error:
        raise ManagerEntryError("the Router executable does not exist") from error
    if not executable.is_file():
        raise ManagerEntryError("the Router executable must be a regular file")
    return [str(executable), *command[1:]]


def run_router(
    command: list[str], drain_seconds: float, shutdown_grace_seconds: float
) -> int | NoReturn:
    command = validate_manager_run(command, drain_seconds, shutdown_grace_seconds)
    try:
        commit = _installed_manager_commit()
    except importlib.metadata.PackageNotFoundError as error:
        raise ManagerEntryError(
            "the pinned vllm-hust-ext Manager is not installed"
        ) from error
    if commit != MANAGER_COMMIT:
        raise ManagerEntryError(
            "the installed Manager is not the reviewed pinned commit"
        )
    # Lazy import: package import and doctor remain inert.
    from vllm_hust_ext.process_supervisor import supervise

    environment = dict(os.environ)
    environment["PREFIX_ROUTING_DRAIN_SECONDS"] = str(drain_seconds)
    return supervise(
        command,
        env=environment,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )
