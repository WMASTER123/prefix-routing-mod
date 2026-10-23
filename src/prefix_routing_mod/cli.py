"""CLI diagnostics deliberately do not claim runtime readiness."""

import argparse
import json
from pathlib import Path

from .adapters.profiles import inspect_backend
from .config import load_config
from .manager import ManagerEntryError, run_router


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate static configuration only")
    doctor.add_argument("--config", type=Path, required=True)
    backend = commands.add_parser(
        "backend-doctor",
        help="check an explicit backend profile without importing engines",
    )
    backend.add_argument("--profile", required=True)
    manager = commands.add_parser(
        "manager-run",
        help="supervise one explicit Router command with the pinned Manager",
    )
    manager.add_argument("--drain-seconds", type=float, required=True)
    manager.add_argument("--shutdown-grace-seconds", type=float, required=True)
    manager.add_argument("router_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "backend-doctor":
        try:
            report = inspect_backend(args.profile)
        except (OSError, ValueError, RuntimeError) as error:
            print(json.dumps({"interface_verified": False, "error": str(error)}))
            return 2
        print(json.dumps(report))
        return 0
    if args.command == "manager-run":
        try:
            return run_router(
                args.router_command,
                args.drain_seconds,
                args.shutdown_grace_seconds,
            )
        except ManagerEntryError as error:
            print(json.dumps({"manager_started": False, "error": str(error)}))
            return 2
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
