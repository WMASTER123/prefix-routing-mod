import copy
import json
from pathlib import Path

import pytest

from prefix_routing_mod.cli import main
from prefix_routing_mod.config import parse_config

EXAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "configs/example.json").read_text("utf-8")
)


def test_valid_pool():
    pool = parse_config(EXAMPLE)
    assert len(pool.workers) == 2
    assert pool.workers[0].worker_id == "replica-a"


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://host",
        "http://user:secret@host",
        "http://host/v1",
        "http://host?key=x",
        "http://host#x",
        "http://host:99999",
        "http://host:0",
        "http://host:",
        "http://ho st",
        "http://",
    ],
)
def test_reject_unsafe_or_ambiguous_origin(endpoint):
    config = copy.deepcopy(EXAMPLE)
    config["workers"][0]["endpoint"] = endpoint
    with pytest.raises(ValueError):
        parse_config(config)


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"workers": []},
        {"model": ""},
        {"compatibility_domain": None},
        {"typo": 1},
    ],
)
def test_reject_malformed_pool(mutation):
    config = copy.deepcopy(EXAMPLE)
    config.update(mutation)
    with pytest.raises(ValueError):
        parse_config(config)


def test_duplicate_origins_are_normalized():
    config = copy.deepcopy(EXAMPLE)
    config["workers"][0]["endpoint"] = "http://LOCALHOST"
    config["workers"][1]["endpoint"] = "http://localhost:80/"
    with pytest.raises(ValueError, match="unique"):
        parse_config(config)


def test_duplicate_ids_rejected():
    config = copy.deepcopy(EXAMPLE)
    config["workers"][1]["worker_id"] = config["workers"][0]["worker_id"]
    with pytest.raises(ValueError, match="unique"):
        parse_config(config)


def test_doctor_is_not_runtime_verification(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(EXAMPLE), encoding="utf-8")
    assert main(["doctor", "--config", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["config_valid"]
    assert not report["routing_ready"]
    assert not report["engine_probed"]
    assert main(["doctor", "--config", str(tmp_path / "missing")]) == 2
    assert not json.loads(capsys.readouterr().out)["config_valid"]
