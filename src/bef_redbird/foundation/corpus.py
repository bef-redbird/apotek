"""Manifest-selected archive operations; backfills and new releases share one path.

The external plan supplies inspected source contracts, never inferred dates from
archive labels. Every version has a disposition; acquisition receipts stay distinct.
The append-only journal is a checkpoint, not a source of analytical authority.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import Store, Timing
from .archive import external_path
from .records import canonical_json
from .source_audit import auditor_fingerprint, reconcile, snapshot_imports


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_inventory(manifest_path: Path, source_root: Path) -> dict[str, Any]:
    """Validate every manifest version/receipt and physical original, without scanning."""
    manifest_path, source_root = external_path(manifest_path), external_path(source_root)
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("items"), dict):
        raise ValueError("Unsupported acquisition manifest")
    files = []
    paths = set()
    receipt_ids = set()
    for key, item in manifest["items"].items():
        if item.get("status") != "complete":
            raise ValueError("Acquisition contains an incomplete item")
        versions = item["versions"]
        version_paths = {v["archive_relative_path"] for v in versions}
        if len(version_paths) != len(versions) or item["current_version"] not in version_paths:
            raise ValueError("Invalid acquisition version catalogue")
        if any(r["archive_relative_path"] not in version_paths for r in item["receipts"]):
            raise ValueError("Acquisition receipt has no inventoried version")
        for version in versions:
            relative = version["archive_relative_path"]
            path = (source_root / relative).resolve()
            if path in paths or not path.is_relative_to(source_root):
                raise ValueError("Duplicate or escaping acquisition path")
            paths.add(path)
            if (
                not path.is_file()
                or path.stat().st_size != version["bytes"]
                or file_hash(path) != version["sha256"]
            ):
                raise ValueError("Original integrity mismatch")
            receipts = [r for r in item["receipts"] if r["archive_relative_path"] == relative]
            if not receipts:
                raise ValueError("Original has no acquisition receipt")
            for receipt in receipts:
                stamp = datetime.fromisoformat(receipt["received_at_utc"])
                if stamp.utcoffset() is None or receipt["receipt_id"] in receipt_ids:
                    raise ValueError("Invalid acquisition receipt identity/timing")
                receipt_ids.add(receipt["receipt_id"])
                if any(receipt[k] != version[k] for k in ("bytes", "sha256", "original_filename")):
                    raise ValueError("Receipt contradicts acquisition version")
                if receipt["item_key"] != key:
                    raise ValueError("Receipt item mismatch")
            with path.open("rb") as stream:
                magic = stream.read(16)
            observed = (
                "pdf"
                if magic.startswith(b"%PDF")
                else "rtf"
                if magic.startswith(b"{\\rtf")
                else "xlsx"
                if magic.startswith(b"PK\x03\x04")
                else "xls"
                if magic.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
                else "unknown"
            )
            if observed != {"xls_ole": "xls"}.get(version["format"], version["format"]):
                raise ValueError("Acquisition format/magic mismatch")
            files.append(
                {
                    "item_key": key,
                    "acquisition_identity": item["identity"],
                    "source_url_sha256": item["source_url_sha256"],
                    "version": version,
                    "path": str(path),
                    "receipts": receipts,
                    "observed_format": observed,
                }
            )
    return {
        "version": 1,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "files": files,
        "file_count": len(files),
        "receipt_count": len(receipt_ids),
        "byte_count": sum(f["version"]["bytes"] for f in files),
    }


def load_plan(path: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    plan = json.loads(external_path(path).read_text(encoding="utf-8"))
    if plan.get("version") != 1 or plan.get("manifest_sha256") != inventory["manifest_sha256"]:
        raise ValueError("Plan must pin the unchanged acquisition manifest")
    entries = plan["files"]
    keys = [entry["archive_relative_path"] for entry in entries]
    expected = {f["version"]["archive_relative_path"] for f in inventory["files"]}
    if len(set(keys)) != len(keys) or set(keys) != expected:
        raise ValueError("Plan must account for every inventoried original exactly once")
    for entry in entries:
        if entry.get("disposition") not in {"ingest", "exclude"}:
            raise ValueError("Unsupported original disposition")
        if not isinstance(entry.get("evidence"), str) or not entry["evidence"].strip():
            raise ValueError("Every disposition needs source-audit evidence")
        if entry["disposition"] == "exclude" and not entry.get("reason"):
            raise ValueError("An exclusion requires a scoped reason")
        if entry["disposition"] == "ingest":
            if not entry.get("adapter") or not entry.get("parser_version"):
                raise ValueError("An import requires an explicit adapter/version")
            if set(entry.get("context", {})) & {"current_period", "target_period"}:
                raise ValueError("Report periods belong to delivery metadata")
    return plan


def _append(path: Path, record: dict[str, Any]) -> None:
    with external_path(path).open("a", encoding="utf-8") as stream:
        stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    # A torn trailing line cannot silently count as a completed checkpoint.
    return [
        json.loads(line) for line in external_path(path).read_text(encoding="utf-8").splitlines()
    ]


def ingest_plan(
    database: Path,
    archive: Path,
    inventory: dict[str, Any],
    plan: dict[str, Any],
    journal: Path,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    if not external_path(database).is_file():
        raise ValueError("Corpus ingestion requires the explicitly initialized existing store")
    items = {item["version"]["archive_relative_path"]: item for item in inventory["files"]}
    old = read_journal(journal)
    done = {
        (r["archive_relative_path"], r["parser_version"], r["interpretation_hash"]): r
        for r in old
        if r["event"] == "imported"
    }
    processed = registered = excluded = 0
    with Store(database, archive) as store:
        # Keep each import's normal durable commit, but amortize full checkpoints
        # across archive files. Closing Store still checkpoints normally; an
        # interrupted owner recovers committed work from the retained WAL.
        store._connection.execute("SET checkpoint_threshold='256MB'")
        for period in plan["calendar"]:
            if not period.get("evidence"):
                raise ValueError("Calendar intervals require source evidence")
            store.add_period(date.fromisoformat(period["start"]), date.fromisoformat(period["end"]))
        for entry in plan["files"]:
            relative = entry["archive_relative_path"]
            item = items[relative]
            if entry["disposition"] == "exclude":
                excluded += 1
                continue
            # New receipts for unchanged bytes must still be registered. They do
            # not change the selected derivation or multiply its observations.
            interpretation_hash = hashlib.sha256(
                canonical_json({"entry": entry, "receipts": item["receipts"]}).encode()
            ).hexdigest()
            checkpoint = (relative, entry["parser_version"], interpretation_hash)
            if checkpoint in done:
                previous = done[checkpoint]
                batch = store.get_record("import_batch", previous["import_id"])
                if str(batch["delivery_id"]) != previous["delivery_id"]:
                    raise ValueError("Checkpoint disagrees with persisted import")
                store.original_path(previous["delivery_id"])
                continue
            if limit is not None and processed >= limit:
                break
            primary = entry["receipt_id"]
            delivery_id = None
            for receipt in item["receipts"]:
                stamp = datetime.fromisoformat(receipt["received_at_utc"])
                metadata = entry.get("metadata", {})
                current = metadata.get("current_period")
                target = metadata.get("target_period")
                coverage = {
                    **metadata.get("coverage", {"version": 1, "kind": "unknown"}),
                    "acquisition": {
                        "item_key": item["item_key"],
                        "identity": item["acquisition_identity"],
                        "receipt": receipt,
                    },
                }
                selected_delivery = store.register_source(
                    Path(item["path"]),
                    source=entry["source"],
                    receipt_key="archive-receipt:" + receipt["receipt_id"],
                    received=Timing(
                        lower=stamp,
                        upper=stamp,
                        precision="instant",
                        basis="downloader acquisition receipt",
                        source_timezone="UTC",
                    ),
                    current_period=date.fromisoformat(current) if current else None,
                    target_period=date.fromisoformat(target) if target else None,
                    coverage=coverage,
                )
                registered += 1
                if receipt["receipt_id"] == primary:
                    delivery_id = selected_delivery
            if delivery_id is None:
                raise ValueError("Chosen interpretation receipt is absent from the inventory")
            result = store.ingest(
                delivery_id,
                adapter=entry["adapter"],
                version=entry["parser_version"],
                mapping_version=entry.get("mapping_version", "f4-source-local-v1"),
                mapping_policy="delivery",
                context=entry.get("context", {}),
            )
            _append(
                journal,
                {
                    "event": "imported",
                    "archive_relative_path": relative,
                    "parser_version": entry["parser_version"],
                    "interpretation_hash": interpretation_hash,
                    "manifest_sha256": inventory["manifest_sha256"],
                    **result,
                },
            )
            processed += 1
    return {
        "processed": processed,
        "receipts_registered_or_reused": registered,
        "excluded": excluded,
    }


def reconcile_journal(
    database: Path,
    archive: Path,
    inventory: dict[str, Any],
    journal: Path,
    output_root: Path,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    output_root = external_path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    files = {f["version"]["archive_relative_path"]: f for f in inventory["files"]}
    imports = {row["import_id"]: row for row in read_journal(journal) if row["event"] == "imported"}
    passed = failed = processed = reused = 0
    for import_id, item in imports.items():
        destination = output_root / f"{import_id}.json.gz"
        if destination.exists():
            report = json.loads(gzip.decompress(destination.read_bytes()))
            if report.get("audit_scope") != {"kind": "explicit_imports", "import_ids": [import_id]}:
                raise ValueError("Existing audit report has a different scope")
            current = snapshot_imports(database, [import_id])
            fingerprints = {
                table: {"count": data["count"], "sha256": data["sha256"]}
                for table, data in current.items()
            }
            if (
                report.get("baseline_tables") != fingerprints
                or report.get("auditor_code") != auditor_fingerprint()
            ):
                raise ValueError("Audit evidence is stale; use a new audit output directory")
            for delivery in current["source_delivery"]["rows"]:
                archived = (archive / delivery["archive_location"]).resolve()
                if not archived.is_relative_to(archive.resolve()) or (
                    file_hash(archived) != delivery["content_hash"]
                ):
                    raise ValueError("Audited archive original changed")
            passed += int(report["complete"])
            failed += int(not report["complete"])
            reused += 1
            continue
        if limit is not None and processed >= limit:
            break
        original = Path(files[item["archive_relative_path"]]["path"])
        report = reconcile(database, archive, [original], import_ids=[import_id])
        temporary = destination.with_name(f".{import_id}.{uuid4()}.partial")
        with temporary.open("xb") as stream:
            with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed:
                compressed.write(json.dumps(report, ensure_ascii=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        # Publish complete evidence without overwriting another run. An interrupted
        # temporary file remains inspectable and cannot masquerade as an audit.
        os.link(temporary, destination)
        temporary.unlink()
        processed += 1
        passed += int(report["complete"])
        failed += int(not report["complete"])
    return {"processed": processed, "reused_reports": reused, "verified": passed, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory_command = commands.add_parser("inventory")
    inventory_command.add_argument("--output", type=Path, required=True)
    for name in ("ingest", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--journal", type=Path, required=True)
        command.add_argument("--limit", type=int)
        if name == "reconcile":
            command.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        inventory = load_inventory(args.manifest, args.source_root)
        if args.command == "inventory":
            with external_path(args.output).open("x", encoding="utf-8") as stream:
                json.dump(inventory, stream, ensure_ascii=False)
            result = {key: inventory[key] for key in ("file_count", "receipt_count", "byte_count")}
        else:
            plan = load_plan(args.plan, inventory)
            if args.limit is not None and args.limit < 0:
                raise ValueError("Limit must be nonnegative")
            if args.command == "ingest":
                result = ingest_plan(
                    args.database, args.archive, inventory, plan, args.journal, limit=args.limit
                )
            else:
                result = reconcile_journal(
                    args.database,
                    args.archive,
                    inventory,
                    args.journal,
                    args.output_root,
                    limit=args.limit,
                )
        print(json.dumps(result, sort_keys=True))
        return int(bool(result.get("failed")))
    except Exception as exc:
        # Keep operational diagnostics beside the private journal; never echo
        # source rows, SQL values or private paths to the command's public output.
        if getattr(args, "journal", None) is not None:
            try:
                _append(
                    args.journal.with_name(args.journal.name + ".failures.jsonl"),
                    {"event": "operation_failed", "type": type(exc).__name__, "detail": str(exc)},
                )
            except OSError:
                pass
        print(json.dumps({"complete": False, "error": "corpus_operation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
