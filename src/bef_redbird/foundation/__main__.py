"""Initialize storage, register unchanged originals, or verify the archive."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from . import FoundationError, Store, Timing
from .archive import external_path


def read_metadata(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    values = json.loads(external_path(path).read_text(encoding="utf-8"))
    allowed = {
        "original_uri",
        "received",
        "published",
        "edition",
        "phase",
        "current_period",
        "target_period",
        "coverage",
        "replacement",
    }
    if not isinstance(values, dict) or values.keys() - allowed:
        raise ValueError("Unsupported registration metadata")
    for key in ("received", "published"):
        if key in values:
            timing = values[key]
            for bound in ("lower", "upper"):
                if timing.get(bound) is not None:
                    timing[bound] = datetime.fromisoformat(timing[bound])
            values[key] = Timing(**timing)
    for key in ("current_period", "target_period"):
        if values.get(key) is not None:
            values[key] = date.fromisoformat(values[key])
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("verify")
    commands.add_parser("adapters")
    period = commands.add_parser("period")
    period.add_argument("start", type=date.fromisoformat)
    period.add_argument("end_exclusive", type=date.fromisoformat)
    register = commands.add_parser("register")
    register.add_argument("file", type=Path)
    register.add_argument("--source", required=True)
    register.add_argument("--receipt-key", required=True)
    register.add_argument("--metadata", type=Path, help="External JSON with evidenced metadata")
    importing = commands.add_parser("import", help="Archive/register and interpret unchanged bytes")
    importing.add_argument("file", type=Path)
    importing.add_argument("--source", required=True)
    importing.add_argument("--receipt-key", required=True)
    importing.add_argument("--metadata", type=Path)
    ingest = commands.add_parser("ingest", help="Interpret an already registered delivery")
    ingest.add_argument("delivery_id")
    for command in (importing, ingest):
        command.add_argument("--adapter", required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--mapping-version", required=True)
        command.add_argument(
            "--mapping-policy", choices=("existing", "delivery"), default="existing"
        )
        command.add_argument("--mapping-scope")
        command.add_argument(
            "--context", required=True, type=Path, help="External interpretation JSON"
        )
    summary = commands.add_parser("summary")
    summary.add_argument("import_id")
    issues = commands.add_parser(
        "issues", help="Safe issue counts; optional private detailed export"
    )
    issues.add_argument("import_id")
    issues.add_argument(
        "--output", type=Path, help="New external JSON file; never writes source files"
    )
    backup = commands.add_parser("backup", help="Copy a closed store before opening/migrating it")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Restore verified backup to new database/archive")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in {"backup", "restore"}:
            from .recovery import backup_store, restore_store

            if args.command == "backup":
                backup_store(args.database, args.archive, args.destination)
            else:
                restore_store(args.backup, args.database, args.archive)
            print("Cold storage copy verified.")
            return 0
        if args.command == "adapters":
            from .ingestion import VERSIONS

            print(json.dumps(VERSIONS, sort_keys=True))
            return 0
        with Store(args.database, args.archive) as store:
            if args.command == "init":
                print("Foundation initialized; migrations verified.")
            elif args.command == "verify":
                counts = store.verify_archive()
                print(json.dumps(counts, sort_keys=True))
                return int(bool(counts["missing"] or counts["corrupt"]))
            elif args.command == "period":
                store.add_period(args.start, args.end_exclusive)
                print("Calendar period registered.")
            elif args.command in {"summary", "issues"}:
                result = store.import_summary(args.import_id)
                if args.command == "issues" and args.output is not None:
                    # Exclusive creation avoids overwriting originals or earlier evidence.
                    with external_path(args.output).open("x", encoding="utf-8") as output:
                        json.dump(store.issues(args.import_id), output, default=str, indent=2)
                print(json.dumps(result, sort_keys=True))
            elif args.command in {"register", "import", "ingest"}:
                if args.command == "ingest":
                    delivery_id = args.delivery_id
                else:
                    delivery_id = store.register_source(
                        args.file,
                        source=args.source,
                        receipt_key=args.receipt_key,
                        **read_metadata(args.metadata),
                    )
                if args.command == "register":
                    print(json.dumps({"delivery_id": delivery_id}))
                    return 0
                context = json.loads(external_path(args.context).read_text(encoding="utf-8"))
                result = store.ingest(
                    delivery_id,
                    adapter=args.adapter,
                    version=args.version,
                    mapping_version=args.mapping_version,
                    mapping_policy=args.mapping_policy,
                    mapping_scope=args.mapping_scope,
                    context=context,
                )
                print(json.dumps(result, sort_keys=True))
                return int(result["validation_status"] != "accepted")
    except (FoundationError, duckdb.Error, OSError, ValueError, TypeError, AttributeError):
        # DB/OS exceptions can contain private values and absolute source paths.
        print(
            "Foundation operation failed; inspect storage and supplied metadata locally.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
