"""Install the built wheel outside the source tree in a clean temporary venv."""

import argparse
import json
import os
import subprocess
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    candidates = list((ROOT / "dist").glob("prefix_routing_mod-*.whl"))
    if args.wheel:
        wheel = args.wheel.resolve()
    elif len(candidates) == 1:
        wheel = candidates[0]
    else:
        parser.error("build exactly one wheel, or provide --wheel")
    with tempfile.TemporaryDirectory(prefix="prefix-mod-wheel-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        binary = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        clean = os.environ.copy()
        clean.pop("PYTHONPATH", None)
        clean.pop("PREFIX_ROUTING_HUST_PUBLISHER", None)
        subprocess.run(
            [str(binary), "-m", "pip", "install", "--no-deps", str(wheel)],
            cwd=root,
            env=clean,
            check=True,
        )
        probe = (
            "import json; from importlib.metadata import entry_points; "
            "from pathlib import Path; "
            "ep = next(iter(entry_points(group='vllm_hust.extension_bundles'))); "
            "module = ep.load(); "
            "p = Path(module.__file__).with_name('vllm-hust-extension-v0.3.json'); "
            "m = json.loads(p.read_text()); "
            "assert m['implementation'][0]['status'] == 'import_only'; "
            "assert m['activation']['entry_points'] == []; "
            "hook = next(iter(entry_points(group='vllm.general_plugins', "
            "name='prefix_routing_publisher'))).load(); hook(); "
            "import sys; assert 'torch' not in sys.modules; "
            "assert 'vllm' not in sys.modules; "
            "from prefix_routing_mod.contracts import CONTRACT_VERSION, WorkerRef; "
            "assert CONTRACT_VERSION == 'component-v0.1'; "
            "assert WorkerRef('replica', 'instance').instance_epoch == 'instance'; "
            "from prefix_routing_mod.adapters.profiles import get_profile; "
            "assert not get_profile('hust-cedb477-native-v1').native_epoch; "
            "assert 'torch' not in sys.modules and 'vllm' not in sys.modules; "
            "print('Clean wheel: descriptor and bundled manifest verified')"
        )
        subprocess.run(
            [str(binary), "-I", "-c", probe], cwd=root, env=clean, check=True
        )
        result = subprocess.run(
            [
                str(binary),
                "-I",
                "-m",
                "prefix_routing_mod.cli",
                "doctor",
                "--config",
                str(ROOT / "configs/example.json"),
            ],
            cwd=root,
            env=clean,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout)["routing_ready"] is False
    report = ROOT / "artifacts/wheel.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "status": "pass",
                "wheel": wheel.name,
                "scope": "clean CPU install; not Manager activation or inference",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
