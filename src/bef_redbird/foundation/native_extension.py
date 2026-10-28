"""Read-only certification gates for a declared append-only native publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import registry_selection as selection
from .archive import external_path, verify
from .lms_registry_audit import AUDITOR_VERSION
from .records import digest
from .store import Store

CONTRACT = "verified-append-only-native-proof-v1"


def checked(reference: dict[str, Any]) -> Path:
    path = external_path(Path(reference["path"]))
    verify(path, reference["sha256"], reference["size_bytes"])
    return path


def parent_binding(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    documents = {
        key: json.loads(checked(request[key]).read_text())
        for key in ("base_backup", "base_recovery", "base_restore", "parent_publication")
    }
    backup, recovery, restored, publication = (
        documents[key]
        for key in ("base_backup", "base_recovery", "base_restore", "parent_publication")
    )
    parent = publication["result"]
    if (
        not recovery.get("complete")
        or not restored.get("complete")
        or not restored.get("verification", {}).get("complete")
        or not restored["verification"].get("schema_unchanged")
        or not parent.get("verified")
        or recovery["store_manifest_sha256"] != request["base_backup"]["sha256"]
        or restored["recovery_manifest_sha256"] != request["base_recovery"]["sha256"]
        or backup["files"]["database.duckdb"] != request["prior_database"]["sha256"]
        or parent["dataset_id"] not in restored["verification"]["datasets"]
        or restored["verification"]["operating_status"]["dataset_id"] != parent["dataset_id"]
    ):
        raise ValueError("Accepted recovery/publication closure changed")
    checked(request["prior_database"])
    return parent, publication


def configuration_scope(old: dict[str, Any], new: dict[str, Any], request: dict[str, Any]) -> None:
    dynamic = {
        "release_import_ids",
        "period_bindings",
        "supply_imports",
        "supplement_import_ids",
        "builder_provenance",
    }
    if {k: v for k, v in old.items() if k not in dynamic} != {
        k: v for k, v in new.items() if k not in dynamic
    }:
        raise ValueError("Inherited semantic/money/identity policy changed")
    for key, declared in (
        ("release_import_ids", request["registry_imports"]),
        ("supplement_import_ids", request["supplement_imports"]),
    ):
        a, b = set(old[key]), set(new[key])
        if not a <= b or b - a != set(declared):
            raise ValueError("Undeclared or removed publication inputs")
    a = {digest(x) for x in old["supply_imports"]}
    b = {digest(x) for x in new["supply_imports"]}
    declared = {
        digest({k: item[k] for k in ("import_id", "verification_delivery_id")})
        for item in request["supply_imports"]
    }
    if not a <= b or b - a != declared:
        raise ValueError("Undeclared or removed supplier pins")
    old_periods = {digest(p) for p in old["period_bindings"]}
    new_periods = {digest(p) for p in new["period_bindings"]}
    last = max(p["period_start"] for p in old["period_bindings"])
    if not old_periods <= new_periods or any(
        p["period_start"] <= last or p["snapshot_date"] != p["period_start"]
        for p in new["period_bindings"]
        if digest(p) not in old_periods
    ):
        raise ValueError("Old period choices changed or new snapshot backdated")


def certify_rows(store: Store, request: dict[str, Any]) -> dict[str, Any]:
    """Compare full typed old rows, then admit only located new dependencies."""
    con = store._connection
    registry = list(request["registry_imports"])
    supplements = list(request["supplement_imports"])
    supply = [x["import_id"] for x in request["supply_imports"]]
    declared = registry + supplements + supply
    if len(declared) != len(set(declared)) or not declared:
        raise ValueError("Distinct new accepted imports required")
    # All interpolation is fixed authored table/column names; source IDs are parameters.
    con.execute(
        "CREATE TEMP TABLE extension_imports AS SELECT unnest(?::UUID[]) import_id", [declared]
    )
    con.execute(
        "CREATE TEMP TABLE extension_registry AS SELECT unnest(?::UUID[]) import_id", [registry]
    )
    con.execute(
        "CREATE TEMP TABLE extension_supplements AS SELECT unnest(?::UUID[]) import_id",
        [supplements],
    )
    con.execute(
        "CREATE TEMP TABLE extension_supply AS SELECT unnest(?::UUID[]) import_id", [supply]
    )
    con.execute(
        "CREATE TEMP TABLE extension_deliveries AS SELECT unnest(?::UUID[]) delivery_id",
        [list(request["source_deliveries"])],
    )
    con.execute(
        "CREATE TEMP TABLE extension_members AS SELECT DISTINCT b.member_id,m.member_number "
        "FROM registry_release_member b JOIN registry_member m USING(member_id) "
        "JOIN extension_registry e USING(import_id)"
    )
    con.execute(
        "CREATE TEMP TABLE extension_rows AS SELECT m.member_number,unnest(m.row_ids) row_id "
        "FROM registry_member m JOIN extension_members e USING(member_id,member_number)"
    )
    con.execute(
        "CREATE TEMP TABLE extension_auditor AS SELECT ? auditor_version", [AUDITOR_VERSION]
    )
    allowed = {
        "import_batch": (
            "import_id IN (SELECT import_id FROM extension_imports) "
            "AND validation_status='accepted'"
        ),
        "source_delivery": "delivery_id IN (SELECT delivery_id FROM extension_deliveries)",
        "registry_release": "import_id IN (SELECT import_id FROM extension_registry)",
        "registry_release_member": "import_id IN (SELECT import_id FROM extension_registry)",
        "registry_release_verification": (
            "import_id IN (SELECT import_id FROM extension_registry) "
            "AND auditor_version=(SELECT auditor_version FROM extension_auditor)"
        ),
        "registry_member": "member_id IN (SELECT member_id FROM extension_members)",
        "registry_member_verification": (
            "member_id IN (SELECT member_id FROM extension_members) "
            "AND auditor_version=(SELECT auditor_version FROM extension_auditor)"
        ),
        "supplement_import": "import_id IN (SELECT import_id FROM extension_supplements)",
        "supplement_record": "import_id IN (SELECT import_id FROM extension_supplements)",
        "supply_observation": "import_id IN (SELECT import_id FROM extension_supply)",
        "package_observation": "import_id IN (SELECT import_id FROM extension_supply)",
        "package_identifier": (
            "evidence_import_id IN (SELECT import_id FROM extension_supply) "
            "AND mapping_version='supplier-source-local-v1' AND validity_kind='scope'"
        ),
        "package": (
            "package_id IN (SELECT package_id FROM package_identifier WHERE "
            "evidence_import_id IN (SELECT import_id FROM extension_supply) "
            "AND mapping_version='supplier-source-local-v1' AND validity_kind='scope')"
        ),
    }
    tables = [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name=current_database() "
            "AND NOT temporary ORDER BY table_name"
        ).fetchall()
    ]
    comparisons = {}
    for table in tables:
        if not table.replace("_", "").isalnum():
            raise ValueError("Unexpected table name")
        old_missing = selection._scalar(
            store,
            f'SELECT count(*) FROM (SELECT * FROM prior."{table}" EXCEPT ALL '
            f'SELECT * FROM main."{table}")',
            [],
        )
        if old_missing:
            raise ValueError("Existing typed rows, money, identity or provenance changed: " + table)
        where = allowed.get(table, "false")
        if table.startswith("registry_lms") and table[12:].isdigit():
            number = int(table[12:])
            where = f"row_id IN (SELECT row_id FROM extension_rows WHERE member_number={number})"
        extra = f'(SELECT * FROM main."{table}" EXCEPT ALL SELECT * FROM prior."{table}")'
        bad = selection._scalar(
            store, f"SELECT count(*) FROM {extra} WHERE NOT coalesce(({where}),false)", []
        )
        if bad:
            raise ValueError("Undeclared added rows: " + table)
        count = selection._scalar(store, f"SELECT count(*) FROM {extra}", [])
        comparisons[table] = {"old_rows_unchanged": True, "declared_added_rows": count}
        print(json.dumps({"certified_table": table, "added": count}), flush=True)
    actual = {
        str(x[0])
        for x in con.execute(
            "SELECT import_id FROM main.import_batch EXCEPT "
            "SELECT import_id FROM prior.import_batch"
        ).fetchall()
    }
    if actual != set(declared):
        raise ValueError("Declared new import inventory differs")
    actual_deliveries = selection._rows(
        store, "SELECT * FROM main.source_delivery EXCEPT SELECT * FROM prior.source_delivery"
    )
    if {str(x["delivery_id"]): digest(selection._serial(x)) for x in actual_deliveries} != request[
        "source_deliveries"
    ]:
        raise ValueError("Declared source receipt/evidence rows differ")
    return comparisons


def main() -> None:
    from .native_reuse import publish_verified_extension

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("request", "proof", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = publish_verified_extension(args.request, args.proof, args.receipt)
    print(json.dumps({"verified": result["verified"], "dataset_id": result["dataset_id"]}))


if __name__ == "__main__":
    main()
