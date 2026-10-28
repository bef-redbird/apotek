"""Controlled FTP-primary copy/cutover, preserving the pre-cleanup recovery authority.

The source is read-only. Each copied table commits independently and is compared
exactly on retry. The destination is never an operating database until publication
and the final receipt succeed. This is a data migration, not an applied-schema edit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from . import registry_selection
from .locking import writer_lock
from .migrations import migrate
from .store import Store

VERSION = "ftp-primary-cutover-v1"
OBSERVATIONS = (
    "price_observation",
    "package_observation",
    "classification_observation",
    "supply_observation",
    "lifecycle_event",
)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _set(con: duckdb.DuckDBPyConnection, name: str, values: set[str]) -> None:
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name}(id UUID PRIMARY KEY)")
    if values:
        con.register("_retention_ids", pa.table({"id": sorted(values)}))
        try:
            con.execute(f"INSERT INTO {name} SELECT CAST(id AS UUID) FROM _retention_ids")
        finally:
            con.unregister("_retention_ids")


def retention(con: duckdb.DuckDBPyConnection, config: dict[str, Any]) -> dict[str, str]:
    """Make an explicit relational + metadata provenance dependency closure."""
    tables = [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name='prior'"
        ).fetchall()
    ]
    releases = con.execute("SELECT import_id,effective_date FROM prior.registry_release").fetchall()
    if any(row[1].year < 2008 for row in releases):
        raise ValueError("Review pre-2008 native releases before using this migration")
    keep_imports = {str(row[0]) for row in releases} | set(config["supplement_import_ids"])
    supply = {item["import_id"] for item in config["supply_imports"]}
    keep_imports |= supply
    keep_imports |= {
        str(r[0])
        for r in con.execute("SELECT import_id FROM prior.registry_partial_group").fetchall()
    }
    _set(con, "keep_supply", supply)
    # Preserve every assertion in admitted supplier imports: their independent
    # proof checks the complete inventory, including package descriptions.
    mappings = {
        str(r[0])
        for table in OBSERVATIONS
        for r in con.execute(
            f"SELECT DISTINCT mapping_id FROM prior.{table} "
            "WHERE import_id IN (SELECT id FROM keep_supply) AND mapping_id IS NOT NULL"
        ).fetchall()
    }
    _set(con, "keep_mapping", mappings)
    packages = {
        str(r[0])
        for r in con.execute(
            "SELECT DISTINCT package_id FROM prior.package_identifier "
            "WHERE mapping_id IN (SELECT id FROM keep_mapping) AND package_id IS NOT NULL"
        ).fetchall()
    }
    for table in OBSERVATIONS:
        packages |= {
            str(r[0])
            for r in con.execute(
                f"SELECT DISTINCT package_id FROM prior.{table} "
                "WHERE import_id IN (SELECT id FROM keep_supply) AND package_id IS NOT NULL"
            ).fetchall()
        }
    _set(con, "keep_package", packages)
    keep_imports |= {
        str(r[0])
        for r in con.execute(
            "SELECT DISTINCT evidence_import_id FROM prior.package_identifier "
            "WHERE mapping_id IN (SELECT id FROM keep_mapping) AND evidence_import_id IS NOT NULL"
        ).fetchall()
    }
    batches = {str(r[0]): r for r in con.execute("SELECT * FROM prior.import_batch").fetchall()}
    deliveries = {
        str(r[0]): r for r in con.execute("SELECT * FROM prior.source_delivery").fetchall()
    }
    evidence = set(UUID_RE.findall(json.dumps(config))) & deliveries.keys()
    # Partial sources and their original parent/verification contexts also survive.
    _set(con, "keep_import", keep_imports)
    for table in ("registry_partial_group", "registry_partial_member", "supplement_import"):
        for row in con.execute(
            f"SELECT * FROM prior.{table} WHERE import_id IN (SELECT id FROM keep_import)"
        ).fetchall():
            evidence |= set(UUID_RE.findall(str(row))) & deliveries.keys()
    while True:
        old = (len(keep_imports), len(evidence))
        refs = set()
        for key in keep_imports:
            refs.update(UUID_RE.findall(str(batches[key])))
        for key in evidence:
            refs.update(UUID_RE.findall(str(deliveries[key])))
        keep_imports |= refs & batches.keys()
        evidence |= refs & deliveries.keys()
        if old == (len(keep_imports), len(evidence)):
            break
    _set(con, "keep_import", keep_imports)
    _set(con, "keep_delivery", evidence)
    predicates = {table: "TRUE" for table in tables}
    for table in OBSERVATIONS:
        predicates[table] = "import_id IN (SELECT id FROM keep_supply)"
    predicates.update(
        {
            "import_batch": "import_id IN (SELECT id FROM keep_import)",
            "import_issue": "import_id IN (SELECT id FROM keep_import)",
            "source_delivery": "delivery_id IN (SELECT id FROM keep_delivery)",
            "package_identifier": "mapping_id IN (SELECT id FROM keep_mapping)",
            "package": "package_id IN (SELECT id FROM keep_package)",
            "frozen_selection": "FALSE",
            "analytical_dataset": "FALSE",
            "price_period": "period_end_exclusive > DATE '2008-01-01' OR period_start IN "
            "(SELECT current_period FROM prior.source_delivery WHERE delivery_id IN "
            "(SELECT id FROM keep_delivery) UNION SELECT target_period FROM prior.source_delivery "
            "WHERE delivery_id IN (SELECT id FROM keep_delivery) UNION SELECT period "
            "FROM prior.import_issue WHERE import_id IN (SELECT id FROM keep_import))",
            "supplement_import": "import_id IN (SELECT id FROM keep_import)",
            "supplement_record": "import_id IN (SELECT id FROM keep_import)",
        }
    )
    for table in tables:
        if table.startswith("registry_dataset_"):
            predicates[table] = "FALSE"
    return predicates


def _order(con: duckdb.DuckDBPyConnection, tables: set[str]) -> list[str]:
    dependencies: dict[str, set[str]] = {t: set() for t in tables}
    for table, target in con.execute(
        "SELECT table_name,referenced_table FROM duckdb_constraints() "
        "WHERE database_name='prior' AND constraint_type='FOREIGN KEY'"
    ).fetchall():
        dependencies[table].add(target)
    result: list[str] = []
    while dependencies:
        ready = sorted(t for t, deps in dependencies.items() if not deps - set(result))
        if not ready:
            raise ValueError("Cyclic schema dependency")
        for table in ready:
            result.append(table)
            del dependencies[table]
    return result


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def exact_compare(con: duckdb.DuckDBPyConnection, table: str, predicate: str) -> int:
    """Full typed-value comparison, not a probabilistic checksum or count alone."""
    name = _quote(table)
    n = _count(con, f"SELECT count(*) FROM prior.{name} WHERE {predicate}")
    actual = _count(con, f"SELECT count(*) FROM main.{name}")
    if n != actual:
        raise ValueError(f"Retained count mismatch: {table}")
    # EXCEPT ALL preserves JSON strings, integer values, arrays and multiplicity.
    difference = _count(
        con,
        f"SELECT count(*) FROM ((SELECT * FROM prior.{name} WHERE {predicate}) "
        f"EXCEPT ALL (SELECT * FROM main.{name}))",
    )
    if difference:
        raise ValueError(f"Retained values/provenance changed: {table}")
    return int(n)


def prepare(
    source: Path, archive: Path, destination: Path, recovery: Path, receipt: Path, dataset_id: str
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        writer_lock(source.with_name(source.name + ".writer")),
        writer_lock(destination.with_name(destination.name + ".writer")),
    ):
        return _prepare(source, archive, destination, recovery, receipt, dataset_id)


def _prepare(
    source: Path, archive: Path, destination: Path, recovery: Path, receipt: Path, dataset_id: str
) -> dict[str, Any]:
    """Build a resumable compact store; keep the old operating pointers untouched."""
    source, archive, destination, recovery, receipt = (
        p.resolve() for p in (source, archive, destination, recovery, receipt)
    )
    if source == destination or destination.is_relative_to(archive):
        raise ValueError("Choose an unused database outside the original archive")
    if source.with_suffix(source.suffix + ".wal").exists():
        raise ValueError("Stop the writer and checkpoint before lean migration")
    backup = json.loads((recovery / "store/backup.json").read_text())
    accepted = json.loads((recovery / "recovery-acceptance.json").read_text())
    if not accepted.get("complete") or not accepted.get("store_byte_verification"):
        raise ValueError("An accepted pre-cleanup recovery set is required")
    started = time.monotonic()
    checkpoint = json.loads(receipt.read_text()) if receipt.exists() else {}
    source_sha = file_hash(source)
    if source_sha != backup["files"]["database.duckdb"]:
        raise ValueError("Source differs from accepted recovery database; review a new baseline")
    binding = {
        "version": VERSION,
        "source_sha256": source_sha,
        "source": str(source),
        "destination": str(destination),
        "archive": str(archive),
        "dataset_id": dataset_id,
        "recovery": str(recovery),
        "recovery_manifest_sha256": file_hash(recovery / "store/backup.json"),
        "recovery_acceptance_sha256": file_hash(recovery / "recovery-acceptance.json"),
    }
    if checkpoint and checkpoint["binding"] != binding:
        raise ValueError("Migration retry differs from its frozen inputs")
    if not checkpoint:
        if destination.exists():
            raise ValueError("Destination exists without this migration's checkpoint")
        checkpoint = {"binding": binding, "tables": {}, "complete": False}
        write_json(receipt, checkpoint)
    destination.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(destination))
    try:
        con.execute("SET memory_limit='8GB'")
        con.execute("SET threads=2")
        con.execute("SET max_temp_directory_size='12GB'")
        con.execute("SET preserve_insertion_order=false")
        migrate(con)
        con.execute("ATTACH '" + str(source).replace("'", "''") + "' AS prior (READ_ONLY)")
        row = con.execute(
            "SELECT configuration FROM prior.analytical_dataset WHERE dataset_id=?", [dataset_id]
        ).fetchone()
        if row is None:
            raise ValueError("Accepted dataset is absent")
        config = json.loads(row[0])
        config["legacy_imports"] = []
        config["legacy_selection_ids"] = []
        config["period_bindings"] = [
            item for item in config["period_bindings"] if item["period_start"] >= "2008-01-01"
        ]
        builder = config.get("builder_provenance", {})
        if "policy" in builder:
            builder["policy"]["legacy_imports"] = []
            builder["policy"]["legacy_selection_ids"] = []
            builder["policy"]["history_from"] = "2008-01-01"
            builder["policy_hash"] = registry_selection.digest(builder["policy"])
        predicates = retention(con, config)
        if not checkpoint.get("expected"):
            checkpoint["expected"] = {
                table: {
                    "before": _count(con, f"SELECT count(*) FROM prior.{_quote(table)}"),
                    "retained": _count(
                        con, f"SELECT count(*) FROM prior.{_quote(table)} WHERE {where}"
                    ),
                    "predicate": where,
                }
                for table, where in predicates.items()
            }
            write_json(receipt, checkpoint)
        for table in _order(con, set(predicates)):
            if table not in checkpoint["tables"]:
                print(f"copy {table}", flush=True)
                con.execute("BEGIN")
                try:
                    con.execute(f"DELETE FROM main.{_quote(table)}")
                    con.execute(
                        f"INSERT INTO main.{_quote(table)} SELECT * FROM prior.{_quote(table)} "
                        f"WHERE {predicates[table]}"
                    )
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            count = exact_compare(con, table, predicates[table])
            checkpoint["tables"][table] = {"retained": count, "exact_values_equal": True}
            write_json(receipt, checkpoint)
        for sequence, last in con.execute(
            "SELECT sequence_name,last_value FROM duckdb_sequences() WHERE database_name='prior'"
        ).fetchall():
            con.execute(f"DROP SEQUENCE {_quote(sequence)}")
            con.execute(f"CREATE SEQUENCE {_quote(sequence)} START {int(last) + 1}")
        con.execute("CHECKPOINT")
        config["legacy_imports"] = []
        config["legacy_selection_ids"] = []
        # The receipt binds the parent dataset. Keep the ordinary policy-builder
        # configuration so an unchanged maintenance refresh has the same identity.
        checkpoint["configuration"] = config
        checkpoint["copy_complete"] = True
        checkpoint["destination_bytes"] = destination.stat().st_size
        checkpoint["source_bytes"] = source.stat().st_size
        checkpoint["elapsed_seconds"] = time.monotonic() - started
        write_json(receipt, checkpoint)
    finally:
        con.close()
    return checkpoint


def publish(receipt: Path, policy: Path | None = None) -> dict[str, Any]:
    value = json.loads(receipt.read_text())
    if not value.get("copy_complete"):
        raise ValueError("Finish and verify the lean copy before publication")
    binding = value["binding"]
    with Store(Path(binding["destination"]), Path(binding["archive"])) as store:
        store._connection.execute("SET memory_limit='8GB'")
        store._connection.execute("SET max_temp_directory_size='12GB'")
        store._connection.execute("SET threads=2")
        store._connection.execute("SET enable_progress_bar=false")
        if policy is not None:
            selected = registry_selection.build_configuration(store, json.loads(policy.read_text()))
            for key in (
                "release_import_ids",
                "period_bindings",
                "semantic_profiles",
                "supply_imports",
                "supplement_import_ids",
                "native_price_value_policy",
            ):
                if selected[key] != value["configuration"][key]:
                    raise ValueError("Reviewed lean publication scope changed: " + key)
            value["configuration"] = selected
            write_json(receipt, value)
        result = registry_selection.publish(store, value["configuration"])
    value["publication"] = result
    value["complete"] = True
    value["completed_utc"] = datetime.now(UTC).isoformat()
    value["destination_sha256"] = file_hash(Path(binding["destination"]))
    write_json(receipt, value)
    return {
        "complete": True,
        "dataset_id": result["dataset_id"],
        "evidence_hash": result["evidence_hash"],
    }


def cutover(
    receipt: Path, config: Path, policy: Path, archive: Path, journal: Path
) -> dict[str, Any]:
    """Verify/link immutable originals and switch current pointers; safe forward retry."""
    proof = json.loads(receipt.read_text())
    if not proof.get("complete") or not proof["publication"].get("verified"):
        raise ValueError("Fully verified lean publication required")
    settings = json.loads(config.read_text())
    operations = Path(settings["operation_root"])
    with writer_lock(operations / "interpretation.writer"):
        if journal.exists():
            transaction = json.loads(journal.read_text())
            if transaction["migration_sha256"] != file_hash(receipt):
                raise ValueError("Cutover migration receipt changed")
        else:
            database = Path(proof["binding"]["destination"])
            if file_hash(database) != proof["destination_sha256"]:
                raise ValueError("Lean database changed before operating cutover")
            old_archive = Path(proof["binding"]["archive"])
            archive = archive.resolve()
            required: set[str] = set()
            with duckdb.connect(str(database), read_only=True) as con:
                required |= {
                    row[0]
                    for row in con.execute("SELECT content_hash FROM source_delivery").fetchall()
                }
            acquired = json.loads((operations / "acquisition-state.json").read_text())
            for item in acquired["files"].values():
                for version in [*item.get("versions", []), *item.get("receipts", [])]:
                    required.add(version["sha256"])
            copying = journal.with_name("source-copy-checkpoint.json")
            copy_binding = {
                "source": str(old_archive),
                "destination": str(archive),
                "required_hash": registry_selection.digest({"hashes": sorted(required)}),
            }
            if copying.exists():
                if json.loads(copying.read_text()) != copy_binding:
                    raise ValueError("Source archive retry inputs changed")
            else:
                if archive.exists():
                    raise ValueError("Use a new source archive destination")
                write_json(copying, copy_binding)
            archive.mkdir(parents=True, exist_ok=True)
            copied = []
            for sha in sorted(required):
                relative = Path("sha256") / sha[:2] / sha
                original, target = old_archive / relative, archive / relative
                if not original.is_file() or original.is_symlink() or file_hash(original) != sha:
                    raise ValueError("Required original is missing or altered")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.is_symlink() or file_hash(target) != sha:
                        raise ValueError("Resumed original archive changed")
                else:
                    os.link(original, target)
                copied.append({"sha256": sha, "bytes": target.stat().st_size})
            source_map = {
                "version": 1,
                "old_archive": str(old_archive),
                "current_archive": str(archive),
                "archived_originals": str(Path(proof["binding"]["recovery"]) / "store/originals"),
                "current_objects": copied,
                "rule": (
                    "Current hashes resolve in current_archive; earlier required source bytes "
                    "resolve in archived_originals. Immutable originals only; "
                    "no mutable database hardlinks."
                ),
            }
            write_json(journal.parent / "source-retention.json", source_map)
            new_settings = {
                **settings,
                "database": str(database),
                "archive_root": str(archive),
                "native_root": str(archive),
            }
            new_policy = json.loads(policy.read_text())
            legacy_path = (
                Path(settings["registry_policy_config"]).parent
                / "source-review/registered-legacy-evidence.json"
            )
            legacy = json.loads(legacy_path.read_text())
            legacy["legacy_imports"] = []
            legacy["supply_imports"] = new_policy["supply_imports"]
            frozen_config = proof["configuration"]
            selection = (
                operations / "selections" / (registry_selection.digest(frozen_config) + ".json")
            )
            configuration_bytes = (registry_selection.canonical_json(frozen_config) + "\n").encode()
            selection.parent.mkdir(parents=True, exist_ok=True)
            if selection.exists() and selection.read_bytes() != configuration_bytes:
                raise ValueError("Frozen selection path contains different bytes")
            if not selection.exists():
                selection.write_bytes(configuration_bytes)
            publication = {
                "version": 1,
                "configuration_path": str(selection),
                "configuration_sha256": file_hash(selection),
                "completed_utc": proof["completed_utc"],
                "result": proof["publication"],
            }
            values = [
                (Path(settings["registry_policy_config"]), new_policy),
                (legacy_path, legacy),
                (operations / "publication-state.json", publication),
                (config, new_settings),
            ]
            transaction = {
                "version": 1,
                "migration_sha256": file_hash(receipt),
                "complete": False,
                "changes": [
                    {"path": str(path.resolve()), "before_sha256": file_hash(path), "after": value}
                    for path, value in values
                ],
            }
            write_json(journal, transaction)
        for item in transaction["changes"]:
            path = Path(item["path"])
            actual = json.loads(path.read_text())
            if actual != item["after"] and file_hash(path) != item["before_sha256"]:
                raise ValueError("Operating configuration changed during cutover")
            if actual != item["after"]:
                write_json(path, item["after"])
        transaction["complete"] = True
        write_json(journal, transaction)
    return {"complete": True, "database": str(Path(proof["binding"]["destination"]))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("prepare")
    for name in ("source", "archive", "destination", "recovery", "receipt"):
        build.add_argument("--" + name, type=Path, required=True)
    build.add_argument("--dataset-id", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--receipt", type=Path, required=True)
    pub.add_argument("--policy", type=Path)
    switch = sub.add_parser("cutover")
    for name in ("receipt", "config", "policy", "archive", "journal"):
        switch.add_argument("--" + name, type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "prepare":
        result = prepare(**args)
    elif command == "publish":
        result = publish(**args)
    else:
        result = cutover(**args)
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k in ("complete", "copy_complete", "dataset_id", "destination_bytes")
            }
        )
    )


if __name__ == "__main__":
    main()
