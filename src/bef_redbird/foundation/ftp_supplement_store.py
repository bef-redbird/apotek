"""Atomic provenance-backed persistence for independently verified supplements."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from .ftp_supplements import VERSION, independent_audit, parse_supplement
from .records import FoundationError, canonical_json, digest
from .store import Store

AUDITOR_VERSION = "ftp-supplement-native-audit-v1"
_FIELDS = (
    "source_line",
    "byte_offset",
    "byte_length",
    "assertion",
    "package_code",
    "drug_id",
    "extended_prescribing_code",
    "issue_date",
    "effective_date",
    "valid_from",
    "valid_to",
)


class SupplementError(FoundationError):
    """Supplement persistence or independent native evidence is inconsistent."""


def supplement_records(store: Store, import_id: str) -> list[dict[str, Any]]:
    """Read every located occurrence and check its indexed columns against payload."""
    store._check_owner()
    result = store._connection.execute(
        "SELECT " + ",".join(_FIELDS) + ",payload FROM supplement_record "
        "WHERE import_id=? ORDER BY source_line",
        [import_id],
    )
    records = []
    for row in result.fetchall():
        payload = json.loads(row[-1])
        for field, actual in zip(_FIELDS, row[:-1], strict=True):
            actual = actual.isoformat() if isinstance(actual, date) else actual
            if actual != payload.get(field):
                raise SupplementError("Supplement indexed column disagrees with source payload")
        records.append(payload)
    return records


def _insert_records(store: Store, import_id: str, records: list[dict[str, Any]]) -> None:
    store._insert_many(
        "supplement_record",
        [
            {
                "import_id": import_id,
                **{field: record.get(field) for field in _FIELDS},
                "payload": canonical_json(record),
            }
            for record in records
        ],
    )


def _proof(data: bytes, kind: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "auditor_version": AUDITOR_VERSION,
        "original_sha256": hashlib.sha256(data).hexdigest(),
        "typed_sha256": digest({"records": records}),
        **independent_audit(data, kind, records),
    }


def _result(
    import_id: str,
    status: str,
    coverage: dict[str, Any],
    proof: dict[str, Any],
    *,
    retry: bool,
) -> dict[str, Any]:
    count = coverage["candidate_rows"]
    return {
        "import_id": import_id,
        "status": status,
        "verification": proof,
        "independently_verified": status == "accepted" and proof["accepted"],
        "counts": {
            "record_count": count,
            "accepted_count": count if status == "accepted" else 0,
            "rejected_count": 0 if status == "accepted" else count,
        },
        "coverage": {
            **coverage,
            "publication_status": "complete" if status == "accepted" else "rejected",
            "published_rows": count if status == "accepted" else 0,
        },
        "retry": retry,
    }


def ingest_supplement(store: Store, delivery_id: str, kind: str) -> dict[str, Any]:
    """Import one registered original; partial/unsupported sources publish no rows.

    Completed imports are immutable. A retry rechecks archived bytes, independently
    audits every persisted accepted occurrence and detects payload/index corruption.
    Unexpected interruptions leave a running import and roll back all publication,
    so the same delivery can resume without a competing import or duplicate facts.
    """
    store._check_owner()
    if kind not in ("DOK", "LMS36", "LMS37"):
        raise ValueError("Unsupported supplementary source kind")
    delivery = store.get_record("source_delivery", delivery_id)
    data = store.original_path(delivery_id).read_bytes()
    native_hash = hashlib.sha256(data).hexdigest()
    if native_hash != delivery["content_hash"] or len(data) != delivery["byte_count"]:
        raise SupplementError("Supplement archived bytes changed during read")
    import_id = store.begin_import(
        delivery_id,
        parser_version=VERSION,
        schema_version="supplement-1",
        mapping_version="native-source-keys-no-identity-inference-v1",
        context={"kind": kind, "auditor_version": AUDITOR_VERSION},
    )
    prior = store.get_record("import_batch", import_id)
    parsed = parse_supplement(data, kind)
    coverage = parsed["coverage"]
    if prior["validation_status"] != "running":
        saved = store._connection.execute(
            "SELECT kind,original_sha256,metadata,coverage,verification,delivery_id "
            "FROM supplement_import WHERE import_id=?",
            [import_id],
        ).fetchone()
        if saved is None or (saved[0], saved[1], str(saved[5])) != (kind, native_hash, delivery_id):
            raise SupplementError("Completed supplement provenance is missing or inconsistent")
        if json.loads(saved[2]) != parsed["metadata"] or json.loads(saved[3]) != coverage:
            raise SupplementError("Completed supplement metadata disagrees with native source")
        records = supplement_records(store, import_id)
        proof = _proof(
            data, kind, records if prior["validation_status"] == "accepted" else parsed["records"]
        )
        if proof != json.loads(saved[4]):
            raise SupplementError("Completed supplement verification disagrees with native source")
        if prior["validation_status"] == "accepted" and not proof["accepted"]:
            raise SupplementError("Completed supplement no longer passes independent audit")
        expected_counts = (
            coverage["candidate_rows"],
            len(records),
            coverage["candidate_rows"] - len(records),
        )
        if (
            prior["record_count"],
            prior["accepted_count"],
            prior["rejected_count"],
        ) != expected_counts:
            raise SupplementError("Completed supplement counts are inconsistent")
        return _result(import_id, prior["validation_status"], coverage, proof, retry=True)

    proof = _proof(data, kind, parsed["records"])
    accepted = coverage["status"] == "complete" and not parsed["issues"] and proof["accepted"]
    with store.transaction():
        store._insert(
            "supplement_import",
            {
                "import_id": import_id,
                "delivery_id": delivery_id,
                "kind": kind,
                "original_sha256": native_hash,
                "metadata": canonical_json(parsed["metadata"]),
                "coverage": canonical_json(coverage),
                "verification": canonical_json(proof),
            },
        )
        if accepted:
            _insert_records(store, import_id, parsed["records"])
            # Publication acceptance uses persisted readback, not in-memory rows.
            readback_proof = _proof(data, kind, supplement_records(store, import_id))
            if readback_proof != proof or not readback_proof["accepted"]:
                raise SupplementError("Supplement publication failed independent readback audit")
        else:
            for issue in parsed["issues"]:
                store.add_issue(
                    import_id,
                    source_locator=(
                        f"line:{issue['source_line']}" if issue["source_line"] else "source"
                    ),
                    code=issue["code"],
                    severity="error",
                    blocking_scope="import",
                )
            if not proof["accepted"]:
                store.add_issue(
                    import_id,
                    source_locator="source",
                    code="supplement_native_audit_failed",
                    severity="error",
                    blocking_scope="import",
                )
        count = coverage["candidate_rows"]
        status = "accepted" if accepted else "rejected"
        store.finish_import(
            import_id,
            status=status,
            record_count=count,
            accepted_count=count if accepted else 0,
            rejected_count=0 if accepted else count,
        )
    return _result(import_id, status, coverage, proof, retry=False)
