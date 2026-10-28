"""Explicit offline commands; no scheduler, database access or training controls UI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import EvaluationConfig, ResearchError, evaluate, validate_dataset, verify_run
from .dataset import read_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    run = commands.add_parser("evaluate")
    run.add_argument("manifest", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--config", type=Path)
    run.add_argument("--lineage", type=Path)
    run.add_argument("--development-parent")
    verify = commands.add_parser("verify")
    verify.add_argument("run", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            result = validate_dataset(args.manifest)
        elif args.command == "verify":
            manifest = verify_run(args.run)
            result = {"status": manifest["status"], "run_fingerprint": manifest["run_fingerprint"]}
        else:
            config = (
                EvaluationConfig(**read_json(args.config)) if args.config else EvaluationConfig()
            )
            manifest = evaluate(
                args.manifest,
                args.output,
                config=config,
                lineage_path=args.lineage,
                development_parent=args.development_parent,
            )
            result = {"status": manifest["status"], "run_fingerprint": manifest["run_fingerprint"]}
    except (ResearchError, OSError, TypeError) as exc:
        # Codes only: never echo private rows, paths or arbitrary parser messages.
        parser.exit(
            2,
            "research_command_failed:"
            + (str(exc) if isinstance(exc, ResearchError) else type(exc).__name__)
            + "\n",
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
