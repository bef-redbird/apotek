"""One explicit derivation at a time, with atomic publication and scoped validation."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .adapters.base import AdapterError, Assertion, Parsed, Problem, identifier
from .records import FoundationError, canonical_json, identity, required

if TYPE_CHECKING:
    from .store import Store

TABLE_FIELDS = {
    "price_observation": (
        "price_type",
        "amount_ore",
        "currency",
        "price_basis",
        "tax_basis",
        "fee_basis",
        "price_role",
    ),
    "package_observation": (
        "name",
        "form",
        "strength",
        "pack_description",
        "company",
        "storage_code",
        "atc",
    ),
    "classification_observation": (
        "classification_context",
        "abc_code",
        "group_namespace",
        "group_code",
        "group_scope",
    ),
    "supply_observation": (
        "observation_date",
        "wholesaler_namespace",
        "wholesaler_identifier",
        "reported_status",
    ),
    "lifecycle_event": ("event_type", "original_source_code", "interpretation_status"),
}
STATES = {"present", "missing", "unreported", "blank", "withdrawn", "invalid", "conflicting"}
VERSIONS = {
    "cito": "cito-workbook-v1",
    "lms-history": "lms-history-workbook-v1",
    "price-changes": "price-changes-workbook-v1",
    "introductions": "introductions-workbook-v1",
    "discontinued": "discontinued-workbook-v1",
    "supply": "supply-workbook-v1",
    "medicine-prices": "medicine-prices-pdf-v1",
    "lms-ftp": "lms-ftp-zip-v1",
}
SUPPORTED_VERSIONS = {adapter: [version] for adapter, version in VERSIONS.items()}
SUPPORTED_VERSIONS["lms-history"].append("lms-history-workbook-v2")
SUPPORTED_VERSIONS["introductions"].append("introductions-workbook-v2")
SUPPORTED_VERSIONS["discontinued"].append("discontinued-workbook-v2")
SUPPORTED_VERSIONS["price-changes"].append("price-changes-workbook-v2")
SUPPORTED_VERSIONS["introductions"].append("introductions-workbook-v3")
for _rtf_adapter in ("price-changes", "introductions", "discontinued"):
    SUPPORTED_VERSIONS[_rtf_adapter].append(f"{_rtf_adapter}-rtf-v1")


def adapter_reader(adapter: str, version: str) -> Callable[[bytes, dict[str, Any]], Parsed]:
    from .adapters import lms_ftp, medicine_pdf, reports, rtf_reports, workbooks

    if version != VERSIONS.get(adapter) and version not in SUPPORTED_VERSIONS.get(adapter, []):
        raise ValueError("Unsupported adapter/version")
    if adapter == "medicine-prices":
        return medicine_pdf.parse_medicine_pdf
    if adapter == "lms-ftp":
        return lms_ftp.parse_lms_ftp
    if version == f"{adapter}-rtf-v1":
        return {
            "price-changes": rtf_reports.parse_changes,
            "introductions": rtf_reports.parse_introductions,
            "discontinued": rtf_reports.parse_discontinued,
        }[adapter]
    if adapter == "lms-history" and version == "lms-history-workbook-v2":
        return workbooks.parse_history_subset
    if adapter == "price-changes" and version == "price-changes-workbook-v2":
        return reports.parse_price_changes_v2
    if adapter == "introductions" and version == "introductions-workbook-v3":
        return reports.parse_introductions_v3
    if adapter == "introductions" and version == "introductions-workbook-v2":
        return reports.parse_introductions_v2
    if adapter == "discontinued" and version == "discontinued-workbook-v2":
        return reports.parse_discontinued_v2
    return {
        "cito": workbooks.parse_cito,
        "lms-history": workbooks.parse_history,
        "price-changes": reports.parse_price_changes,
        "introductions": reports.parse_introductions,
        "discontinued": reports.parse_discontinued,
        "supply": reports.parse_supply,
    }[adapter]


def _serial(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


def _mapping_snapshot(store: Store, version: str) -> list[dict[str, Any]]:
    result = store._connection.execute(
        """SELECT m.* FROM package_identifier m JOIN import_batch i
        ON i.import_id = m.evidence_import_id
        WHERE m.mapping_version = ? AND i.validation_status = 'accepted'
        ORDER BY m.mapping_id""",
        [required(version)],
    )
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def _resolve(
    assertion: Assertion, mappings: list[dict[str, Any]], scope: str | None
) -> tuple[str, str] | None:
    canonical = identifier(assertion.source_identifier)
    # A quoted previous price's report date does not prove its unknown effective date.
    on_date = assertion.effective_date or assertion.effective_period
    matches = [
        row
        for row in mappings
        if row["namespace"] == "dk:varenummer"
        and row["canonical_value"] == canonical
        and (
            (row["validity_kind"] == "scope" and scope is not None and row["scope_key"] == scope)
            or (
                row["validity_kind"] == "interval"
                and on_date is not None
                and row["valid_from"] <= on_date < row["valid_to"]
            )
        )
    ]
    if not matches or any(row["package_id"] is None for row in matches):
        return None
    if len({str(row["package_id"]) for row in matches}) != 1:
        raise ValueError("ambiguous_mapping")
    return str(matches[0]["package_id"]), str(matches[0]["mapping_id"])


def _business_key(assertion: Assertion) -> tuple[Any, ...]:
    values = assertion.values
    common = (
        assertion.table,
        identifier(assertion.source_identifier),
        assertion.effective_period,
        assertion.report_period,
        assertion.effective_date,
    )
    keys = {
        "price_observation": (
            "price_type",
            "currency",
            "price_basis",
            "tax_basis",
            "fee_basis",
            "price_role",
        ),
        "package_observation": (),
        "classification_observation": ("classification_context",),
        "supply_observation": ("observation_date", "wholesaler_namespace", "wholesaler_identifier"),
        "lifecycle_event": ("event_type",),
    }
    return (*common, *(values.get(key) for key in keys[assertion.table]))


def _conflicts(parsed: Parsed) -> None:
    groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, assertion in enumerate(parsed.assertions):
        groups[_business_key(assertion)].append(index)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        fields: dict[str, set[str]] = defaultdict(set)
        for index in indexes:
            item = parsed.assertions[index]
            for key in TABLE_FIELDS[item.table]:
                state = item.presence.get(
                    key, "present" if item.values.get(key) is not None else "unreported"
                )
                if state in {"present", "blank", "withdrawn", "conflicting"}:
                    fields[key].add(
                        canonical_json({"state": state, "value": _serial(item.values.get(key))})
                    )
        conflicting = {key for key, values in fields.items() if len(values) > 1}
        if not conflicting:
            continue
        for index in indexes:
            item = parsed.assertions[index]
            presence = dict(item.presence)
            for key in sorted(conflicting):
                if presence.get(
                    key, "present" if item.values.get(key) is not None else "unreported"
                ) not in {
                    "present",
                    "blank",
                    "withdrawn",
                    "conflicting",
                }:
                    continue
                presence[key] = "conflicting"
                parsed.issues.append(
                    Problem(
                        item.source_locator,
                        "conflicting_assertions",
                        identifier=item.source_identifier,
                        field=key,
                        period=item.effective_period,
                    )
                )
            parsed.assertions[index] = replace(item, presence=presence, quality_state="conflicting")


def _validate_assertion(item: Assertion) -> str:
    if item.table not in TABLE_FIELDS or item.values.keys() - set(TABLE_FIELDS[item.table]):
        raise FoundationError("Adapter produced unsupported business fields")
    required(item.source_locator)
    identifier(item.source_identifier)
    raw_json = canonical_json(item.raw)
    if set(item.presence.values()) - STATES:
        raise FoundationError("Adapter produced unsupported presence state")
    for value in (item.effective_date, item.effective_period, item.report_period):
        if value is not None and type(value) is not date:
            raise FoundationError("Adapter produced invalid business date")
    if item.quality_state not in {"valid", "incomplete", "invalid", "conflicting"}:
        raise FoundationError("Adapter produced unsupported quality state")
    if item.table == "price_observation":
        amount = item.values.get("amount_ore")
        if amount is not None and (
            type(amount) is not int or not 0 <= amount <= 9223372036854775807
        ):
            raise FoundationError("Adapter produced invalid exact money")
        if item.values.get("price_role") == "reported_previous_price" and item.effective_period:
            raise FoundationError("Quoted previous price cannot invent an effective period")
    return raw_json


def _publish(
    store: Store,
    import_id: str,
    delivery_id: str,
    assertion: Assertion,
    package_id: str,
    mapping_id: str,
    buffer: dict[str, list[dict[str, Any]]] | None = None,
    *,
    raw_json: str | None = None,
) -> None:
    fields = TABLE_FIELDS[assertion.table]
    presence = {
        key: assertion.presence.get(
            key, "present" if assertion.values.get(key) is not None else "unreported"
        )
        for key in fields
    }
    presence.update(assertion.presence)
    quality = assertion.quality_state
    if quality == "valid" and "invalid" in presence.values():
        quality = "invalid"
    elif quality == "valid" and any(
        state in {"blank", "missing", "withdrawn"} for state in presence.values()
    ):
        quality = "incomplete"
    record = {
        "import_id": import_id,
        "delivery_id": delivery_id,
        "package_id": package_id,
        "mapping_id": mapping_id,
        "source_locator": assertion.source_locator,
        "identifier_namespace": "dk:varenummer",
        "source_identifier": assertion.source_identifier,
        "canonical_identifier": identifier(assertion.source_identifier),
        "effective_period": _serial(assertion.effective_period),
        "report_period": _serial(assertion.report_period),
        "effective_date": _serial(assertion.effective_date),
        "quality_state": quality,
        "field_presence": canonical_json(presence),
        "source_values": canonical_json(assertion.raw) if raw_json is None else raw_json,
        **{key: _serial(assertion.values.get(key)) for key in fields},
    }
    observation_id = identity("observation", {"table": assertion.table, **record})
    complete_record = {"observation_id": observation_id, **record}
    if buffer is None:
        store._insert(assertion.table, complete_record)
    else:
        buffer[assertion.table].append(complete_record)


def _delivery_mappings(
    store: Store,
    import_id: str,
    delivery_id: str,
    mapping_version: str,
    assertions: list[Assertion],
    periods: set[date],
) -> dict[str, tuple[str, str]]:
    """Create the same delivery-scoped identities in one constrained transaction."""
    from .store import UNKNOWN_TIMING

    first: dict[str, str] = {}
    for item in assertions:
        if any(
            p is not None and p not in periods for p in (item.effective_period, item.report_period)
        ):
            continue
        first.setdefault(identifier(item.source_identifier), item.source_locator)
    scope = f"delivery:{delivery_id}"
    existing = store._connection.execute(
        """SELECT m.canonical_value, m.package_id FROM package_identifier m
        JOIN import_batch i ON i.import_id=m.evidence_import_id
        WHERE m.namespace='dk:varenummer' AND m.mapping_version=?
        AND m.validity_kind='scope' AND m.scope_key=?
        AND i.validation_status IN ('running', 'accepted')""",
        [mapping_version, scope],
    ).fetchall()
    packages = []
    mappings = []
    resolved = {}
    for canonical, locator in first.items():
        package_id = identity(
            "source-package",
            {
                "delivery_id": delivery_id,
                "namespace": "dk:varenummer",
                "identifier": canonical,
            },
        )
        record = {
            "package_id": package_id,
            "namespace": "dk:varenummer",
            "identifier_value": canonical,
            "canonical_value": canonical,
            "validity_kind": "scope",
            "valid_from": None,
            "valid_to": None,
            "scope_key": scope,
            "evidence_import_id": import_id,
            "source_locator": locator,
            "mapping_version": required(mapping_version),
        }
        mapping_id = identity("mapping", {**record, "available": UNKNOWN_TIMING.metadata()})
        packages.append({"package_id": package_id})
        mappings.append(
            {
                "mapping_id": mapping_id,
                **record,
                **store._timing_columns("available", UNKNOWN_TIMING.values()),
            }
        )
        resolved[canonical] = package_id, mapping_id
    for canonical, old_package in existing:
        if canonical in resolved and str(old_package) != resolved[canonical][0]:
            raise ValueError("Ambiguous overlapping identifier mapping")
    store._insert_many("package", packages, existing_ok=True)
    store._insert_many("package_identifier", mappings, existing_ok=True)
    return resolved


def _issue(
    store: Store,
    import_id: str,
    issue: Problem,
    periods: set[date],
    buffer: dict[str, dict[str, Any]] | None = None,
) -> None:
    # Unknown calendar slots cannot be fabricated to satisfy an issue's foreign key.
    scope = issue.blocking_scope
    period = issue.period if issue.period in periods else None
    if scope == "period" and period is None:
        scope = "field"
    affected_identifier = issue.identifier
    if affected_identifier is not None:
        try:
            affected_identifier = identifier(affected_identifier)
        except ValueError:
            pass  # Invalid source identifiers remain actionable through their locator.
    field = issue.field or ("effective_period" if scope == "field" else None)
    if buffer is None:
        store.add_issue(
            import_id,
            source_locator=issue.source_locator,
            code=issue.code,
            blocking_scope=scope,
            severity=issue.severity,
            identifier=affected_identifier,
            field=field,
            period=period,
        )
    else:
        record = {
            "import_id": str(UUID(import_id)),
            "source_locator": required(issue.source_locator),
            "code": required(issue.code),
            "severity": issue.severity,
            "blocking_scope": scope,
            "identifier": required(affected_identifier)
            if affected_identifier is not None
            else None,
            "field": required(field) if field is not None else None,
            "period": period.isoformat() if period else None,
        }
        issue_id = identity("issue", record)
        buffer[issue_id] = {"issue_id": issue_id, **record}


def _validate_lms_delivery(store: Store, delivery: dict[str, Any], context: dict[str, Any]) -> None:
    """ZIP controls cannot establish trusted release phase or source population."""
    from .adapters.report_basis import validated_basis_override

    if delivery["phase"] != "final":
        raise AdapterError("unsupported_lms_release_phase", "manifest:phase")
    if not delivery["edition"] or delivery["target_period"] is not None:
        raise AdapterError("invalid_lms_release_binding", "manifest:delivery")
    calendar = store._connection.execute(
        "SELECT period_end_exclusive FROM price_period WHERE period_start = ?",
        [delivery["current_period"]],
    ).fetchone()
    if calendar is None or calendar[0].isoformat() != context.get("period_end_exclusive"):
        raise AdapterError("lms_calendar_interval_conflict", "manifest:context")
    coverage = json.loads(delivery["coverage"])
    expected_fields = {
        "aip",
        "registered",
        "consumer",
        "reimbursement_basis",
        "abc_code",
        "group_code",
    }
    fields = coverage.get("fields")
    if (
        coverage.get("kind") not in ("partial_extract", "complete_snapshot")
        or coverage.get("family") != "lms-ftp"
        or coverage.get("population") != "lms02-package-set"
        or not isinstance(fields, list)
        or any(not isinstance(field, str) for field in fields)
        or len(fields) != len(expected_fields)
        or set(fields) != expected_fields
        or coverage.get("filter")
    ):
        raise AdapterError("lms_explicit_coverage_required", "manifest:coverage")
    if "price_basis_override" in context:
        profile = validated_basis_override(context)
        try:
            evidence = store.get_record("source_delivery", profile["evidence_delivery_id"])
        except (ValueError, KeyError) as exc:
            raise AdapterError("lms_basis_evidence_missing", "manifest:context") from exc
        if evidence["content_hash"] != profile["evidence_sha256"]:
            raise AdapterError("lms_basis_evidence_hash_conflict", "manifest:context")
        store.original_path(str(evidence["delivery_id"]))


def ingest(
    store: Store,
    delivery_id: str,
    *,
    adapter: str,
    version: str,
    mapping_version: str,
    context: dict[str, Any],
    mapping_policy: str = "existing",
    mapping_scope: str | None = None,
) -> dict[str, Any]:
    store._check_owner()
    if store._depth:
        raise ValueError("Ingest owns its publication transaction")
    reader = adapter_reader(adapter, version)
    delivery = store.get_record("source_delivery", delivery_id)
    delivery_id = str(delivery["delivery_id"])
    if adapter == "lms-history" and version == "lms-history-workbook-v2":
        coverage = json.loads(delivery["coverage"])
        expected_filter = {
            "subset_identifiers": context.get("subset_identifiers"),
            "subset_periods": context.get("subset_periods"),
        }
        if coverage.get("kind") != "partial_extract" or coverage.get("filter") != expected_filter:
            raise ValueError("Bounded history requires matching explicit partial-extract coverage")
    if mapping_policy not in {"existing", "delivery"}:
        raise ValueError("Unsupported mapping policy")
    if mapping_policy == "delivery" and mapping_scope is not None:
        raise ValueError("Delivery mappings use the delivery's scope")
    if mapping_scope is not None and mapping_scope.startswith("delivery:"):
        mapping_scope = f"delivery:{UUID(mapping_scope.removeprefix('delivery:'))}"
        if mapping_scope != f"delivery:{delivery_id}":
            raise ValueError("Delivery-scoped mapping cannot be reused for a different delivery")
    canonical_json(context)
    if "price_basis_override" in context and version not in {
        "price-changes-workbook-v2",
        "introductions-workbook-v3",
        "lms-ftp-zip-v1",
    }:
        raise ValueError("Price basis override requires an explicit supported report version")
    # Trusted delivery context cannot be overwritten through an adapter option.
    if set(context) & {"current_period", "target_period"}:
        raise ValueError("Report periods belong to the delivery metadata")
    effective_context = {
        **context,
        "current_period": _serial(delivery["current_period"]),
        "target_period": _serial(delivery["target_period"]),
    }
    mappings = _mapping_snapshot(store, mapping_version) if mapping_policy == "existing" else []
    interpretation = {
        "version": 1,
        "adapter": adapter,
        "adapter_context": effective_context,
        "mapping_policy": mapping_policy,
        "mapping_scope": mapping_scope,
        "mapping_snapshot": [str(row["mapping_id"]) for row in mappings],
    }
    import_id = store.begin_import(
        delivery_id,
        parser_version=version,
        schema_version="typed-observations-v1",
        mapping_version=mapping_version,
        context=interpretation,
    )
    if store.get_record("import_batch", import_id)["validation_status"] != "running":
        _verify_import_originals(store, import_id)
        return import_summary(store, import_id)
    # Expected format failures become immutable rejected imports. Unexpected process,
    # IO, DB or programming failures leave a running import that can safely retry.
    try:
        if delivery["byte_count"] > 128 * 1024 * 1024:
            raise AdapterError("source_resource_limit")
        data = store.original_path(delivery_id).read_bytes()
        parsed = reader(data, effective_context)
        if adapter == "lms-ftp":
            _validate_lms_delivery(store, delivery, effective_context)
    except AdapterError as exc:
        parsed = Parsed(issues=[Problem(exc.source_locator, exc.code, blocking_scope="import")])
    periods = {
        row[0]
        for row in store._connection.execute("SELECT period_start FROM price_period").fetchall()
    }
    # Validation serializes every raw fragment. Reuse those exact bytes during
    # publication instead of walking/encoding large PDF/RTF provenance twice.
    # Conflict detection changes presence/quality only, never raw fragments.
    raw_fragments = [_validate_assertion(item) for item in parsed.assertions]
    _conflicts(parsed)
    coverage = json.loads(delivery["coverage"])
    if coverage.get("family") not in (None, adapter):
        parsed.issues.append(
            Problem("manifest:coverage", "coverage_family_conflict", blocking_scope="import")
        )
    if coverage.get("kind") == "complete_snapshot" and coverage.get("filter"):
        parsed.issues.append(
            Problem("manifest:coverage", "filtered_snapshot_conflict", blocking_scope="import")
        )
    if coverage.get("kind") not in {
        "unknown",
        "complete_snapshot",
        "changes",
        "patch",
        "partial_extract",
    }:
        parsed.issues.append(
            Problem("manifest:coverage", "unsupported_coverage_kind", blocking_scope="import")
        )
    # Expected counts are declarations, never inferred from omission or archive gaps.
    if "record_count" in coverage and coverage["record_count"] != parsed.records:
        parsed.issues.append(
            Problem("manifest:coverage", "declared_record_count_conflict", blocking_scope="import")
        )
    with store.transaction():
        issue_buffer: dict[str, dict[str, Any]] | None = (
            {} if len(parsed.assertions) + len(parsed.issues) >= 128 else None
        )
        for problem in parsed.issues:
            _issue(store, import_id, problem, periods, issue_buffer)
        fatal = any(problem.blocking_scope == "import" for problem in parsed.issues)
        accepted = 0
        rejected = 0
        delivery_mappings: dict[str, tuple[str, str]] = {}
        # Small imports retain the simple path; corpus-sized imports use the same
        # records/IDs and transaction with bounded vector insertion.
        buffer: dict[str, list[dict[str, Any]]] | None = (
            defaultdict(list) if len(parsed.assertions) >= 128 else None
        )
        if not fatal and mapping_policy == "delivery" and buffer is not None:
            delivery_mappings = _delivery_mappings(
                store, import_id, delivery_id, mapping_version, parsed.assertions, periods
            )
        verified_mapping_deliveries = {delivery_id}
        resolved: tuple[str, str] | None
        if not fatal:
            for item, raw_json in zip(parsed.assertions, raw_fragments, strict=True):
                unknown_period = next(
                    (
                        p
                        for p in (item.effective_period, item.report_period)
                        if p is not None and p not in periods
                    ),
                    None,
                )
                if unknown_period is not None:
                    _issue(
                        store,
                        import_id,
                        Problem(
                            item.source_locator,
                            "unknown_calendar_period",
                            identifier=item.source_identifier,
                            field="effective_period",
                        ),
                        periods,
                        issue_buffer,
                    )
                    rejected += 1
                    continue
                canonical = identifier(item.source_identifier)
                if mapping_policy == "delivery":
                    if canonical not in delivery_mappings:
                        package = store.create_package(
                            identity(
                                "source-package",
                                {
                                    "delivery_id": delivery_id,
                                    "namespace": "dk:varenummer",
                                    "identifier": canonical,
                                },
                            )
                        )
                        mapping = store.add_identifier(
                            package,
                            namespace="dk:varenummer",
                            identifier_value=canonical,
                            evidence_import_id=import_id,
                            source_locator=item.source_locator,
                            mapping_version=mapping_version,
                            validity_kind="scope",
                            scope_key=f"delivery:{delivery_id}",
                        )
                        delivery_mappings[canonical] = package, mapping
                    resolved = delivery_mappings[canonical]
                else:
                    try:
                        resolved = _resolve(item, mappings, mapping_scope)
                    except ValueError:
                        resolved = None
                        code = "ambiguous_mapping"
                    else:
                        code = "unresolved_mapping"
                    if resolved is None:
                        _issue(
                            store,
                            import_id,
                            Problem(
                                item.source_locator,
                                code,
                                blocking_scope="identifier",
                                identifier=item.source_identifier,
                            ),
                            periods,
                            issue_buffer,
                        )
                        rejected += 1
                        continue
                assert resolved is not None
                if mapping_policy == "existing":
                    _verify_mapping_original(store, resolved[1], verified_mapping_deliveries)
                _publish(
                    store,
                    import_id,
                    delivery_id,
                    item,
                    *resolved,
                    buffer=buffer,
                    raw_json=raw_json,
                )
                accepted += 1
            if buffer is not None:
                for table, records in buffer.items():
                    store._insert_many(table, records)
        else:
            rejected = len(parsed.assertions)
        if issue_buffer is not None:
            store._insert_many("import_issue", list(issue_buffer.values()), existing_ok=True)
        # Counts measure assertion candidates, not worksheet rows. Fatal parse failure
        # may yield zero candidates; its import-level issue remains explicit.
        store.finish_import(
            import_id,
            status="accepted" if not fatal and (accepted or parsed.records == 0) else "rejected",
            record_count=accepted + rejected,
            accepted_count=accepted,
            rejected_count=rejected,
        )
    return import_summary(store, import_id)


def import_summary(store: Store, import_id: str) -> dict[str, Any]:
    batch = store.get_record("import_batch", import_id)
    counts = {}
    for table in TABLE_FIELDS:
        count = store._connection.execute(
            f"SELECT count(*) FROM {table} WHERE import_id = ?", [import_id]
        ).fetchone()
        assert count is not None
        counts[table] = count[0]
    issues = store._connection.execute(
        """SELECT code, severity, blocking_scope, count(*) FROM import_issue
        WHERE import_id = ? GROUP BY code, severity, blocking_scope
        ORDER BY code, severity, blocking_scope""",
        [import_id],
    ).fetchall()
    quality = store._connection.execute(
        " UNION ALL ".join(
            f"SELECT quality_state FROM {table} WHERE import_id = ?" for table in TABLE_FIELDS
        ),
        [import_id] * len(TABLE_FIELDS),
    ).fetchall()
    coverage = json.loads(
        store.get_record("source_delivery", str(batch["delivery_id"]))["coverage"]
    )
    limited = (
        batch["validation_status"] != "accepted"
        or batch["rejected_count"] != 0
        or any(row[2] != "none" for row in issues)
        or any(row[0] != "valid" for row in quality)
    )
    return {
        "import_id": str(batch["import_id"]),
        "delivery_id": str(batch["delivery_id"]),
        "parser_version": batch["parser_version"],
        "mapping_version": batch["mapping_version"],
        "validation_status": batch["validation_status"],
        "record_count": batch["record_count"],
        "accepted_count": batch["accepted_count"],
        "rejected_count": batch["rejected_count"],
        "observations": counts,
        "issues": [
            {"code": code, "severity": severity, "blocking_scope": scope, "count": count}
            for code, severity, scope, count in issues
        ],
        "declared_coverage_kind": coverage.get("kind", "unknown"),
        "coverage_assessment": "incomplete" if limited else "declaration_only",
    }


def observations(store: Store, import_id: str, table: str) -> list[dict[str, Any]]:
    if table not in TABLE_FIELDS:
        raise ValueError("Unsupported observation table")
    batch = store.get_record("import_batch", import_id)
    if batch["validation_status"] != "accepted":
        raise ValueError("Only accepted derivations expose observations")
    _verify_import_originals(store, import_id)
    result = store._connection.execute(
        f"SELECT * FROM {table} WHERE import_id = ? ORDER BY source_locator, observation_id",
        [import_id],
    )
    columns = [column[0] for column in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def _verify_mapping_original(store: Store, mapping_id: str, verified: set[str]) -> None:
    mapping = store.get_record("package_identifier", mapping_id)
    evidence = store.get_record("import_batch", str(mapping["evidence_import_id"]))
    delivery_id = str(evidence["delivery_id"])
    if delivery_id not in verified:
        store.original_path(delivery_id)
        verified.add(delivery_id)


def _verify_import_originals(store: Store, import_id: str) -> None:
    batch = store.get_record("import_batch", import_id)
    deliveries = {str(batch["delivery_id"])}
    if batch["parser_version"] == "lms-ftp-zip-v1":
        adapter_context = json.loads(batch["interpretation_context"])["adapter_context"]
        profile = adapter_context.get("price_basis_override")
        if profile:
            evidence = store.get_record("source_delivery", profile["evidence_delivery_id"])
            if evidence["content_hash"] != profile["evidence_sha256"]:
                raise FoundationError("Pinned LMS monetary evidence hash changed")
            deliveries.add(str(evidence["delivery_id"]))
    mappings = " UNION ".join(
        f"SELECT mapping_id FROM {table} WHERE import_id = ?" for table in TABLE_FIELDS
    )
    deliveries.update(
        str(row[0])
        for row in store._connection.execute(
            f"""SELECT DISTINCT i.delivery_id FROM ({mappings}) used
            JOIN package_identifier m ON m.mapping_id = used.mapping_id
            JOIN import_batch i ON i.import_id = m.evidence_import_id""",
            [import_id] * len(TABLE_FIELDS),
        ).fetchall()
    )
    for delivery_id in sorted(deliveries):
        store.original_path(delivery_id)
