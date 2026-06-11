from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config
from .kanboard import KanboardError
from .worker import Worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kanboard-agent-worker")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file")
    parser.add_argument("--log-level", default="INFO", help="Python logging level")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate config and Kanboard connectivity")
    subparsers.add_parser("once", help="Claim and run available work once")
    subparsers.add_parser("run", help="Run the polling worker continuously")

    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format="%(levelname)s %(message)s")

    try:
        config = load_config(args.config)
        worker = Worker(config)

        if args.command == "check":
            for line in worker.check():
                print(line)
            return 0
        if args.command == "once":
            return worker.run_once()
        if args.command == "run":
            worker.run_forever()
            return 0
    except (ConfigError, KanboardError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130

    return 2
