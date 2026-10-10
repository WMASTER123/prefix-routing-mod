import os
import subprocess
import sys


def test_real_manager_rejects_enable_without_writing_config(tmp_path):
    config = tmp_path / "manager.json"
    env = os.environ.copy()
    env["VLLM_HUST_EXT_CONFIG"] = str(config)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from vllm_hust_ext.cli import main; raise SystemExit(main())",
            "extension",
            "enable",
            "io.github.wmaster123.prefix-routing-mod",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "descriptor-only" in result.stdout + result.stderr
    assert not config.exists()
