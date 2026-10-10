"""CLI diagnostics deliberately do not claim runtime readiness."""

import argparse
import json
from pathlib import Path

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate static configuration only")
    doctor.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as error:
        print(json.dumps({"config_valid": False, "error": str(error)}))
        return 2
    print(
        json.dumps(
            {
                "status": "scaffold",
                "config_valid": True,
                "worker_count": len(config.workers),
                "routing_ready": False,
                "manager_activation_ready": False,
                "engine_probed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
