"""Static configuration only; validation never probes endpoints."""

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    endpoint: str


@dataclass(frozen=True)
class PoolConfig:
    model: str
    compatibility_domain: str
    workers: tuple[WorkerConfig, ...]


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _keys(value: object, required: set[str], field: str) -> dict:
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"{field} requires exactly {sorted(required)}")
    return value


def parse_config(value: object) -> PoolConfig:
    raw = _keys(
        value, {"schema_version", "model", "compatibility_domain", "workers"}, "pool"
    )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1")
    model = _text(raw["model"], "model")
    domain = _text(raw["compatibility_domain"], "compatibility_domain")
    if not isinstance(raw["workers"], list) or not raw["workers"]:
        raise ValueError("workers must be a nonempty list")
    workers = []
    identities = set()
    endpoints = set()
    for item in raw["workers"]:
        item = _keys(item, {"worker_id", "endpoint"}, "worker")
        identity = _text(item["worker_id"], "worker_id")
        endpoint = _text(item["endpoint"], "endpoint")
        if any(char.isspace() for char in endpoint):
            raise ValueError("endpoint cannot contain whitespace")
        url = urlsplit(endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("endpoint must be an HTTP(S) origin without credentials")
        port = url.port  # validates port range and syntax
        if port == 0 or url.netloc.endswith(":"):
            raise ValueError("endpoint requires a valid nonzero port")
        normalized = (
            url.scheme,
            url.hostname.lower(),
            port or (443 if url.scheme == "https" else 80),
        )
        if identity in identities or normalized in endpoints:
            raise ValueError("worker IDs and endpoint origins must be unique")
        identities.add(identity)
        endpoints.add(normalized)
        workers.append(WorkerConfig(identity, endpoint.rstrip("/")))
    return PoolConfig(model, domain, tuple(workers))


def load_config(path: Path) -> PoolConfig:
    return parse_config(json.loads(path.read_text(encoding="utf-8")))
