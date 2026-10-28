"""Explicit F3 field selection. No latest-import, name join or implicit replacement.

The stored artifact includes retained input records and field decisions. Version
two retains an explicit identifier catalogue; version one retains every input.
Source reconciliation is a separate, mandatory gate: an archived independent
report must bind verified observation IDs to their exact persisted record hashes.
Such reports establish fidelity, not source authority or historical availability.
"""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .ingestion import TABLE_FIELDS
from .records import (
    FoundationError,
    canonical_identifier,
    canonical_json,
    digest,
    identity,
    required,
)

if TYPE_CHECKING:
    from .store import Store

POLICY = "f3-selection-v1"
FIELDS = {
    "price_observation": ("amount_ore",),
    "package_observation": TABLE_FIELDS["package_observation"],
    "classification_observation": ("abc_code", "group"),
    "supply_observation": ("reported_status",),
    "lifecycle_event": ("event",),
}
CONTEXT = {
    "price_observation": (
        "price_type",
        "currency",
        "price_basis",
        "tax_basis",
        "fee_basis",
        "price_role",
    ),
    "package_observation": (),
    "classification_observation": ("classification_context", "classification_phase"),
    "supply_observation": ("wholesaler_namespace", "wholesaler_identifier"),
    "lifecycle_event": ("event_type",),
}
SCOPE_KEYS = {"tables", "package_ids", "periods", "dates", "fields", "context"}


def serial(value: Any) -> Any:
    """Portable exact record encoding shared with independent report producers."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False, allow_nan=False))


def record_hash(record: dict[str, Any]) -> str:
    return digest(serial(record))


def _ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("Expected explicit ID list")
    normalized = [str(UUID(value)) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("Duplicate explicit ID")
    return sorted(normalized)


def _scope(value: Any, *, complete: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() - SCOPE_KEYS:
        raise ValueError("Unsupported selection scope")
    result = dict(value)
    if complete and not {"tables", "package_ids", "periods", "dates"} <= result.keys():
        raise ValueError("Scope requires explicit tables, packages, periods and dates")
    for key in result:
        if key == "context":
            if not isinstance(result[key], dict):
                raise ValueError("Scope context must be an object")
            known = {item for fields in CONTEXT.values() for item in fields}
            known |= {"group_namespace", "group_scope", "coverage_kind", "phase", "source"}
            if result[key].keys() - known:
                raise ValueError("Unknown business context")
            for items in result[key].values():
                if (
                    not isinstance(items, list)
                    or not items
                    or any(not isinstance(item, str) for item in items)
                ):
                    raise ValueError("Context requires nonempty explicit string lists")
        elif key == "package_ids":
            result[key] = _ids(result[key])
        else:
            if not isinstance(result[key], list) or any(
                not isinstance(item, str) for item in result[key]
            ):
                raise ValueError("Scope requires explicit string lists")
            result[key] = sorted(set(result[key]))
            if key in {"periods", "dates"}:
                for item in result[key]:
                    date.fromisoformat(item)
            if key == "tables" and set(result[key]) - FIELDS.keys():
                raise ValueError("Unsupported observation table")
            if key == "fields" and set(result[key]) - {
                item for fields in FIELDS.values() for item in fields
            }:
                raise ValueError("Unsupported field")
    if complete and (not result["tables"] or not result["package_ids"]):
        raise ValueError("Selection scope cannot be unbounded")
    return result


def _configuration(value: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(canonical_json(value))
    keys = {
        "version",
        "policy_version",
        "import_ids",
        "mapping_ids",
        "derivations",
        "scope",
        "cutoff",
        "mode",
        "availability_policy",
        "source_preference",
        "verification_reports",
        "revisions",
        "identity_scope",
        "business_context",
        "context_evidence",
    }
    mandatory = keys - {"identity_scope"}
    if value.get("version") == 2:
        keys.add("candidate_catalogue")
        mandatory.add("candidate_catalogue")
    if value.keys() - keys or mandatory - value.keys():
        raise ValueError("Selection configuration has missing or unsupported fields")
    if value["version"] not in (1, 2) or value["policy_version"] != POLICY:
        raise ValueError("Unsupported selection version")
    if value["version"] == 2:
        value["candidate_catalogue"] = _catalogue(value["candidate_catalogue"])
        if value["revisions"] != []:
            raise ValueError("Catalogue selections do not support revision operations")
    if value["mode"] not in {"live", "historical_replay", "reconstructed_research"}:
        raise ValueError("Unsupported availability mode")
    if value["availability_policy"] not in {"receipt", "publication"}:
        raise ValueError("Choose receipt or publication explicitly")
    if value["mode"] == "live" and value["availability_policy"] != "receipt":
        raise ValueError("Live selection requires proven operation receipt")
    cutoff = datetime.fromisoformat(value["cutoff"])
    if cutoff.utcoffset() is None:
        raise ValueError("Decision cutoff requires timezone")
    value["cutoff"] = cutoff.astimezone(UTC).isoformat()
    for key in ("import_ids", "mapping_ids", "verification_reports", "revisions"):
        value[key] = _ids(value[key])
    if not value["import_ids"]:
        raise ValueError("Select imports explicitly")
    value["scope"] = _scope(value["scope"], complete=True)
    if not isinstance(value["context_evidence"], dict):
        raise ValueError("Context evidence must pin each import's supporting deliveries")
    value["context_evidence"] = {
        str(UUID(key)): _ids(items) for key, items in value["context_evidence"].items()
    }
    if set(value["context_evidence"]) != set(value["import_ids"]):
        raise ValueError("Declare context evidence for every selected import")
    if not isinstance(value["derivations"], dict):
        raise ValueError("Derivations must explicitly select one import per delivery")
    value["derivations"] = {str(UUID(k)): str(UUID(v)) for k, v in value["derivations"].items()}
    if "identity_scope" in value:
        required(value["identity_scope"])
        if value["identity_scope"].startswith("delivery:"):
            raise ValueError("Delivery scopes are checked against each actual delivery")
    if not isinstance(value["business_context"], dict) or not value["business_context"]:
        raise ValueError("Document the business context and assumptions explicitly")
    preference = value["source_preference"]
    if not isinstance(preference, dict) or set(preference) != {"version", "rules"}:
        raise ValueError("Source preference needs an explicit version and rules")
    required(preference["version"])
    if not isinstance(preference["rules"], list):
        raise ValueError("Preference rules must be a list")
    for rule in preference["rules"]:
        if set(rule) != {"scope", "sources", "evidence_delivery_id", "source_locator"}:
            raise ValueError("Preference rule must have scoped evidence")
        rule["scope"] = _scope(rule["scope"])
        rule["evidence_delivery_id"] = str(UUID(rule["evidence_delivery_id"]))
        required(rule["source_locator"])
        if (
            not isinstance(rule["sources"], list)
            or not rule["sources"]
            or len(set(rule["sources"])) != len(rule["sources"])
        ):
            raise ValueError("Preference must explicitly order unique sources")
        for source in rule["sources"]:
            required(source)
    return value


def _catalogue(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("Candidate catalogue requires explicit identifier pairs")
    pairs = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"namespace", "canonical_identifier"}:
            raise ValueError("Invalid candidate catalogue entry")
        namespace, identifier = item["namespace"], item["canonical_identifier"]
        if canonical_identifier(namespace, identifier) != identifier:
            raise ValueError("Candidate catalogue identifiers must already be canonical")
        pairs.append((namespace, identifier))
    if len(set(pairs)) != len(pairs):
        raise ValueError("Duplicate candidate catalogue identifier")
    return [
        {"namespace": namespace, "canonical_identifier": identifier}
        for namespace, identifier in sorted(pairs)
    ]


def _matches(cell: dict[str, Any], scope: dict[str, Any]) -> bool:
    for plural, singular in (
        ("tables", "table"),
        ("package_ids", "package_id"),
        ("fields", "field"),
    ):
        if plural in scope and cell.get(singular) not in scope[plural]:
            return False
    # Empty dates/periods select none of that kind, not an unbounded interval.
    if cell.get("period") is not None:
        if "periods" in scope and cell["period"] not in scope["periods"]:
            return False
    elif "dates" in scope and cell.get("date") not in scope["dates"]:
        return False
    return all(cell.get(key) in values for key, values in scope.get("context", {}).items())


class Selector:
    def __init__(self, store: Store, config: dict[str, Any]) -> None:
        self.store = store
        self.config = config
        self.now = datetime.now(UTC)
        self.cutoff = datetime.fromisoformat(config["cutoff"])
        self.bound = min(self.cutoff, self.now) if config["mode"] == "live" else self.cutoff
        self.research = config["mode"] == "reconstructed_research"
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.imports: dict[str, dict[str, Any]] = {}
        self.mappings: dict[str, dict[str, Any]] = {}
        self.original_mappings: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.exclusions: list[dict[str, Any]] = []
        self.disclosures: list[dict[str, Any]] = []
        self.coverage: list[dict[str, Any]] = []
        self.cells: list[dict[str, Any]] = []
        self.verified: dict[str, set[str]] = defaultdict(set)
        self.verified_mappings: dict[str, set[str]] = defaultdict(set)
        self.verified_imports: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def delivery(self, delivery_id: str) -> dict[str, Any]:
        delivery_id = str(delivery_id)
        if delivery_id not in self.deliveries:
            self.store.original_path(delivery_id)
            self.deliveries[delivery_id] = serial(
                self.store.get_record("source_delivery", delivery_id)
            )
        return self.deliveries[delivery_id]

    def batch(self, import_id: str) -> dict[str, Any]:
        if import_id not in self.imports:
            self.imports[import_id] = serial(self.store.get_record("import_batch", import_id))
            self.delivery(self.imports[import_id]["delivery_id"])
        return self.imports[import_id]

    def available(self, record: dict[str, Any], prefix: str, kind: str, key: str) -> bool:
        upper = record.get(f"{prefix}_upper")
        reason = (
            "unknown_availability"
            if upper is None
            else "after_cutoff"
            if datetime.fromisoformat(upper) > self.bound
            else None
        )
        if reason is None:
            return True
        detail = {"kind": kind, "id": key, "reason": reason, "clock": prefix}
        if self.research:
            if detail not in self.disclosures:
                self.disclosures.append(detail)
            return True
        if detail not in self.exclusions:
            self.exclusions.append(detail)
        return False

    def delivery_available(self, delivery_id: str) -> bool:
        prefix = "received" if self.config["availability_policy"] == "receipt" else "published"
        return self.available(self.delivery(delivery_id), prefix, "delivery", delivery_id)

    def evidence_available(self, delivery_id: str, locator: str) -> bool:
        required(locator)
        return self.delivery_available(delivery_id)

    def verification(
        self,
        observation_ids: set[str] | None = None,
        mapping_ids: set[str] | None = None,
    ) -> None:
        for report_id in self.config["verification_reports"]:
            self.delivery(report_id)
            report_bytes = self.store.original_path(report_id).read_bytes()
            if report_bytes.startswith(b"\x1f\x8b"):
                report_bytes = gzip.decompress(report_bytes)
            report = json.loads(report_bytes)
            if report.get("version") != 1:
                raise ValueError("Unsupported independent verification report")
            if (
                report.get("complete") is not True
                or report.get("mismatches")
                or report.get("quarantined_observation_ids")
            ):
                raise ValueError("Independent verification report is incomplete or failed")
            ids = set(_ids(report["verified_observation_ids"]))
            hashes = report["observation_hashes"]
            if not isinstance(hashes, dict) or set(hashes) != ids:
                raise ValueError("Verification report requires exact hashes for every verified ID")
            for observation_id, value in hashes.items():
                if not isinstance(value, str) or len(value) != 64:
                    raise ValueError("Invalid observation verification hash")
                if observation_ids is None or observation_id in observation_ids:
                    self.verified[observation_id].add(value)
            for mapping_id, value in report.get("mapping_hashes", {}).items():
                if not isinstance(value, str) or len(value) != 64:
                    raise ValueError("Invalid mapping verification hash")
                mapping_id = str(UUID(mapping_id))
                if mapping_ids is None or mapping_id in mapping_ids:
                    self.verified_mappings[mapping_id].add(value)
            for info in report.get("imports", []):
                if info.get("status") == "verified" and info.get("mismatch_count") == 0:
                    self.verified_imports[str(UUID(info["import_id"]))].append(info)

    def mapping_candidates(self, row: dict[str, Any]) -> Iterable[dict[str, Any]]:
        return self.mappings.values()

    def identity_document(self, delivery_id: str) -> dict[str, Any]:
        return json.loads(self.store.original_path(delivery_id).read_text(encoding="utf-8"))

    def identity_entries(
        self, document: dict[str, Any], mapping: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            entry
            for entry in document["entries"]
            if entry["source_locator"] == mapping["source_locator"]
            and entry["package_id"] == mapping["package_id"]
            and entry["canonical_identifier"] == mapping["canonical_value"]
            and entry["namespace"] == mapping["namespace"]
        ]

    def mapping(self, row: dict[str, Any]) -> dict[str, Any] | None:
        on_date = row["effective_date"] or row["effective_period"]
        candidates = []
        for mapping in self.mapping_candidates(row):
            if mapping["namespace"] != row["identifier_namespace"] or (
                mapping["canonical_value"] != row["canonical_identifier"]
            ):
                continue
            scoped = mapping["validity_kind"] == "scope" and mapping["scope_key"] in {
                f"delivery:{row['delivery_id']}",
                self.config.get("identity_scope"),
            }
            dated = (
                mapping["validity_kind"] == "interval"
                and on_date is not None
                and mapping["valid_from"] <= on_date < mapping["valid_to"]
            )
            if scoped or dated:
                candidates.append(mapping)
        # Resolve ambiguity before availability filtering: a late competing interpretation
        # must not cause an arbitrary available mapping to be chosen from a mixed catalogue.
        if not candidates or any(item["package_id"] is None for item in candidates):
            return None
        if len({item["package_id"] for item in candidates}) != 1:
            return None
        eligible = []
        for item in candidates:
            if record_hash(item) not in self.verified_mappings.get(item["mapping_id"], set()):
                continue
            evidence = self.batch(item["evidence_import_id"])
            if evidence["validation_status"] != "accepted":
                continue
            if evidence["schema_version"] == "f3-identity-evidence-v1":
                document = self.identity_document(evidence["delivery_id"])
                # This bounded evidence format expressly enumerates its source scope.
                if row["delivery_id"] not in document["allowed_delivery_ids"]:
                    continue
                entries = self.identity_entries(document, item)
                if (
                    len(entries) != 1
                    or row["observation_id"] not in entries[0]["supporting_observation_ids"]
                ):
                    continue
                supports_available = [
                    self.delivery_available(delivery_id)
                    for delivery_id in document["supporting_delivery_ids"]
                ]
                if not all(supports_available):
                    continue
            own_available = self.available(item, "available", "mapping", item["mapping_id"])
            source_available = self.delivery_available(evidence["delivery_id"])
            if own_available and source_available:
                eligible.append(item)
        return min(eligible, key=lambda item: item["mapping_id"]) if eligible else None

    def exclude(self, row: dict[str, Any], reason: str, field: str | None = None) -> None:
        self.exclusions.append(
            {
                "kind": "observation",
                "observation_id": row["observation_id"],
                "import_id": row["import_id"],
                "field": field,
                "reason": reason,
            }
        )

    def _fields(self, table: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        presence = json.loads(row["field_presence"])
        base = {
            "table": table,
            "package_id": row["selected_package_id"],
            "period": row["effective_period"],
            "date": row["effective_date"],
            "source": self.delivery(row["delivery_id"])["source"],
            "phase": self.delivery(row["delivery_id"])["phase"],
            "coverage_kind": json.loads(self.delivery(row["delivery_id"])["coverage"]).get("kind"),
            **{key: row.get(key) for key in CONTEXT[table]},
        }
        if table == "price_observation" and row["price_role"] == "reported_previous_price":
            # A quoted amount remains in its report context; it cannot fill an effective slot.
            base.update(period=row["report_period"], date=None)
        if table == "classification_observation":
            base["classification_phase"] = (
                "unknown"
                if row["report_period"] is None
                else "current"
                if row["effective_period"] == row["report_period"]
                else "upcoming"
            )
        if table == "supply_observation":
            base.update(period=None, date=row["observation_date"])
        if table == "lifecycle_event":
            base.update(period=None)
        result = []
        for field in FIELDS[table]:
            keys = (
                ("group_namespace", "group_scope", "group_code")
                if field == "group"
                else ("event_type", "original_source_code", "interpretation_status")
                if field == "event"
                else (field,)
            )
            states = {presence.get(key, "unreported") for key in keys}
            state = next(
                (
                    item
                    for item in (
                        "conflicting",
                        "invalid",
                        "withdrawn",
                        "blank",
                        "missing",
                        "unreported",
                    )
                    if item in states
                ),
                "present",
            )
            value = row[keys[0]] if len(keys) == 1 else {key: row[key] for key in keys}
            if field == "event":
                # Source codes remain evidence, not part of the semantic event value.
                value = row["event_type"]
            cell = {
                **base,
                "field": field,
                "state": state,
                "value": value,
                "observation_id": row["observation_id"],
                "import_id": row["import_id"],
                "mapping_id": row["selected_mapping_id"],
                "original_mapping_id": row["mapping_id"],
                "keys": list(keys),
            }
            if field == "group":
                cell.update(group_namespace=row["group_namespace"], group_scope=row["group_scope"])
            if table == "price_observation":
                unknown_basis = row["tax_basis"] == "unknown" or row["fee_basis"] == "unknown"
                cell["basis_evidence_scope"] = row["delivery_id"] if unknown_basis else None
                cell["limited_comparability"] = unknown_basis
            result.append(cell)
        return result

    def issue_blocks(self, row: dict[str, Any], cell: dict[str, Any]) -> bool:
        for issue in self.issues[row["import_id"]]:
            scope = issue["blocking_scope"]
            if scope == "none":
                continue
            if (
                issue["code"] == "unknown_attribute_period"
                and cell["table"] != "package_observation"
            ):
                continue
            if (
                issue["identifier"] is not None
                and issue["identifier"] != row["canonical_identifier"]
            ):
                continue
            if issue["period"] is not None and issue["period"] != row["effective_period"]:
                continue
            if scope == "field" and issue["source_locator"] != row["source_locator"]:
                # Cell and row locators can refer to the same physical source row.
                def anchor(locator: str) -> tuple[str, str] | None:
                    match = re.fullmatch(r"sheet:(.+)!(?:row:|[A-Z]+)([0-9]+)", locator)
                    return (match[1], match[2]) if match else None

                issue_anchor = anchor(issue["source_locator"])
                if issue_anchor is None or issue_anchor != anchor(row["source_locator"]):
                    continue
            if scope == "field" and issue["field"] not in cell["keys"] + [
                "effective_period",
                "effective_date",
                *CONTEXT[cell["table"]],
            ]:
                continue
            return True
        return False

    def observation_rows(self, import_id: str, table: str) -> Iterable[dict[str, Any]]:
        result = self.store._connection.execute(
            f"SELECT * FROM {table} WHERE import_id = ? ORDER BY observation_id", [import_id]
        )
        columns = [column[0] for column in result.description]
        return [serial(dict(zip(columns, data, strict=True))) for data in result.fetchall()]

    def load(self) -> None:
        self.verification()
        for mapping_id in self.config["mapping_ids"]:
            self.mappings[mapping_id] = serial(
                self.store.get_record("package_identifier", mapping_id)
            )
            self.batch(self.mappings[mapping_id]["evidence_import_id"])
        groups: dict[str, list[str]] = defaultdict(list)
        for import_id in self.config["import_ids"]:
            batch = self.batch(import_id)
            groups[batch["delivery_id"]].append(import_id)
        choices = self.config["derivations"]
        if set(choices) != set(groups) or any(
            choices[delivery] not in ids for delivery, ids in groups.items()
        ):
            raise ValueError("Pin exactly one active derivation for each selected delivery")
        for import_id in self.config["import_ids"]:
            batch = self.batch(import_id)
            self.issues[import_id] = serial(self.store.issues(import_id))
            delivery = self.delivery(batch["delivery_id"])
            active = choices[batch["delivery_id"]] == import_id
            adapter_context = json.loads(batch["interpretation_context"]).get("adapter_context", {})
            if "price_basis_override" in adapter_context:
                profile = adapter_context["price_basis_override"]
                if not isinstance(profile, dict):
                    raise ValueError("Invalid declared price basis evidence")
                evidence_id = str(UUID(profile["evidence_delivery_id"]))
                if evidence_id not in self.config["context_evidence"][import_id]:
                    raise ValueError(
                        "Declared price basis evidence must be pinned in context_evidence"
                    )
                if self.delivery(evidence_id)["content_hash"] != profile["evidence_sha256"]:
                    raise ValueError(
                        "Declared price basis evidence hash differs from pinned delivery"
                    )
            available = self.delivery_available(batch["delivery_id"])
            context_available = [
                self.delivery_available(evidence_id)
                for evidence_id in self.config["context_evidence"][import_id]
            ]
            available = available and all(context_available)
            self.coverage.append(
                {
                    "import_id": import_id,
                    "active": active,
                    "declaration": json.loads(delivery["coverage"]),
                    "replacement_declaration": json.loads(delivery["replacement"]),
                    "rejected_count": batch["rejected_count"],
                    "validation_status": batch["validation_status"],
                    "assessment": "declared_only",
                }
            )
            for table in FIELDS:
                for row in self.observation_rows(import_id, table):
                    oid = row["observation_id"]
                    self.observations[oid] = {"table": table, "record": row}
                    original_mapping_id = row["mapping_id"]
                    if original_mapping_id not in self.original_mappings:
                        original_mapping = serial(
                            self.store.get_record("package_identifier", original_mapping_id)
                        )
                        self.original_mappings[original_mapping_id] = original_mapping
                        self.batch(original_mapping["evidence_import_id"])
                    if not active:
                        self.exclude(row, "alternative_derivation")
                        continue
                    if batch["validation_status"] != "accepted":
                        self.exclude(row, "unaccepted_import")
                        continue
                    if not available:
                        self.exclude(row, "delivery_unavailable")
                        continue
                    if record_hash(row) not in self.verified.get(oid, set()):
                        self.exclude(row, "not_independently_verified")
                        continue
                    if record_hash(self.original_mappings[original_mapping_id]) not in (
                        self.verified_mappings.get(original_mapping_id, set())
                    ):
                        self.exclude(row, "original_mapping_not_independently_verified")
                        continue
                    mapping = self.mapping(row)
                    if mapping is None:
                        self.exclude(row, "unresolved_or_unavailable_identity")
                        continue
                    interpreted = {
                        **row,
                        "selected_package_id": mapping["package_id"],
                        "selected_mapping_id": mapping["mapping_id"],
                    }
                    for cell in self._fields(table, interpreted):
                        if cell["period"] is None and cell["date"] is None:
                            self.exclude(row, "unknown_effective_context", cell["field"])
                        elif not _matches(cell, self.config["scope"]):
                            self.exclude(row, "outside_business_scope", cell["field"])
                        elif (
                            table == "lifecycle_event"
                            and row["interpretation_status"] != "established"
                        ):
                            self.exclude(row, "unestablished_lifecycle_semantics", cell["field"])
                        elif table == "classification_observation" and (
                            cell["classification_phase"] == "unknown"
                        ):
                            self.exclude(row, "unknown_classification_context", cell["field"])
                        elif self.issue_blocks(row, cell):
                            cell["blocked"] = "blocking_source_issue"
                            self.cells.append(cell)
                        elif cell["state"] in {"invalid", "conflicting"}:
                            cell["blocked"] = "invalid_or_conflicting_assertion"
                            self.cells.append(cell)
                        else:
                            self.cells.append(cell)

    def revisions(self) -> None:
        """Apply explicit directed operations; list order never supplies precedence."""
        operations = []
        for evidence_id in self.config["revisions"]:
            self.delivery(evidence_id)
            operation = json.loads(
                self.store.original_path(evidence_id).read_text(encoding="utf-8")
            )
            allowed = {
                "version",
                "kind",
                "replacement_import",
                "supersedes",
                "scope",
                "expected_observation_ids",
                "expected_members",
                "members",
                "source_locator",
            }
            if set(operation) != allowed or operation["version"] != 1:
                raise ValueError("Unsupported revision evidence")
            if operation["kind"] not in {"complete_snapshot", "patch", "changes"}:
                raise ValueError("Unknown revision semantics cannot be applied")
            operation["replacement_import"] = str(UUID(operation["replacement_import"]))
            operation["supersedes"] = _ids(operation["supersedes"])
            operation["scope"] = _scope(operation["scope"], complete=True)
            if not operation["scope"].get("fields"):
                raise ValueError("Replacement scope must explicitly declare fields")
            operation["expected_observation_ids"] = _ids(operation["expected_observation_ids"])
            if not isinstance(operation["members"], dict) or not isinstance(
                operation["expected_members"], list
            ):
                raise ValueError("Release members must be explicit")
            operation["members"] = {
                required(key): str(UUID(value)) for key, value in operation["members"].items()
            }
            operation["evidence_id"] = evidence_id
            operations.append(operation)
        # Topological ordering, rejecting cycles and two operations claiming one replacement.
        targets = [op["replacement_import"] for op in operations]
        if len(targets) != len(set(targets)):
            raise ValueError("Ambiguous replacement operations")
        pending = sorted(operations, key=lambda op: op["evidence_id"])
        completed: set[str] = set()
        while pending:
            ready = [op for op in pending if not (set(op["supersedes"]) & set(targets)) - completed]
            if not ready:
                raise ValueError("Cyclic revision evidence")
            for op in ready:
                self._revision(op)
                completed.add(op["replacement_import"])
                pending.remove(op)

    def _revision(self, op: dict[str, Any]) -> None:
        target = op["replacement_import"]
        scope = op["scope"]
        reasons: list[str] = []
        ids = {target, *op["supersedes"], *op["members"].values()}
        if not ids <= set(self.config["import_ids"]):
            raise ValueError("Revision imports must be pinned in the selection")
        if target in op["supersedes"] or not op["supersedes"]:
            raise ValueError("Replacement needs distinct explicit predecessors")
        if any(self.config["derivations"][self.batch(i)["delivery_id"]] != i for i in ids):
            reasons.append("revision_inactive_derivation")
        if not self.evidence_available(op["evidence_id"], op["source_locator"]):
            reasons.append("revision_evidence_unavailable")
        declared = json.loads(self.delivery(self.batch(target)["delivery_id"])["coverage"])
        if declared.get("kind") != op["kind"] or (
            op["kind"] == "complete_snapshot" and declared.get("filter")
        ):
            reasons.append("revision_coverage_unestablished")
        if (
            len(op["expected_members"]) != len(set(op["expected_members"]))
            or set(op["expected_members"]) != set(op["members"])
            or target not in op["members"].values()
            or len(set(op["members"].values())) != len(op["members"])
            or len({self.batch(i)["delivery_id"] for i in op["members"].values()})
            != len(op["members"])
        ):
            reasons.append("incomplete_release")
        new_imports = set(op["members"].values())
        new = [
            cell
            for cell in self.cells
            if cell["import_id"] in new_imports and _matches(cell, scope)
        ]
        # Derive scope from the FULL persisted member inventories, before admission.
        # Otherwise a missing mapping/verification could silently erase an old key.
        raw_scope: set[tuple[str, str]] = set()
        for observation_id, item in self.observations.items():
            row, table = item["record"], item["table"]
            if row["import_id"] not in new_imports or table not in scope["tables"]:
                continue
            mapped = self.mapping(row)
            if mapped is None:
                # Conservatively refuse replacement when member identity is unresolved.
                reasons.append("revision_unresolved_member_identity")
                continue
            interpreted = {
                **row,
                "selected_package_id": mapped["package_id"],
                "selected_mapping_id": mapped["mapping_id"],
            }
            for cell in self._fields(table, interpreted):
                if _matches(cell, scope):
                    raw_scope.add((observation_id, cell["field"]))
        surviving_scope = {(cell["observation_id"], cell["field"]) for cell in new}
        if {key[0] for key in raw_scope} != set(
            op["expected_observation_ids"]
        ) or raw_scope != surviving_scope:
            reasons.append("revision_assertion_inventory_mismatch")
        target_delivery = self.delivery(self.batch(target)["delivery_id"])
        for import_id in new_imports:
            batch = self.batch(import_id)
            inventory = [
                item["record"]
                for item in self.observations.values()
                if item["record"]["import_id"] == import_id
            ]
            proofs = self.verified_imports.get(import_id, [])
            if not any(
                proof.get("import_record_hash") == record_hash(batch)
                and proof.get("expected_assertions")
                == proof.get("actual_assertions")
                == len(inventory)
                and proof.get("expected_issues")
                == proof.get("actual_issues")
                == len(self.issues[import_id])
                and isinstance(proof.get("source_rows"), int)
                and proof["source_rows"] >= 0
                and proof.get("independent_schema")
                for proof in proofs
            ) or any(
                record_hash(row) not in self.verified.get(row["observation_id"], set())
                for row in inventory
            ):
                reasons.append("revision_source_completeness_unverified")
            if any(issue["blocking_scope"] != "none" for issue in self.issues[import_id]):
                # Some rejected physical rows produce issues but no assertion candidate.
                # Their absence cannot be used as evidence of a snapshot omission.
                reasons.append("revision_unresolved_source_rows")
            member_delivery = self.delivery(batch["delivery_id"])
            member_coverage = json.loads(member_delivery["coverage"])
            if len(op["members"]) > 1:
                member_name = next(name for name, iid in op["members"].items() if iid == import_id)
                if (
                    member_coverage.get("member") != member_name
                    or member_delivery["edition"] is None
                ):
                    reasons.append("revision_member_identity_unestablished")
            if member_coverage.get("kind") != op["kind"] or (
                op["kind"] == "complete_snapshot" and member_coverage.get("filter")
            ):
                reasons.append("revision_coverage_unestablished")
            if any(
                member_delivery[key] != target_delivery[key]
                for key in ("source", "edition", "phase", "current_period", "target_period")
            ):
                reasons.append("revision_member_context_mismatch")
            if batch["validation_status"] != "accepted" or batch["rejected_count"] != 0:
                reasons.append("incomplete_release")
            if not self.delivery_available(batch["delivery_id"]):
                reasons.append("revision_delivery_unavailable")
        if any(cell.get("blocked") or cell["state"] in {"invalid", "conflicting"} for cell in new):
            reasons.append("revision_invalid_scope")
        if op["kind"] == "complete_snapshot" and any(
            cell["state"] in {"missing", "unreported"} for cell in new
        ):
            reasons.append("revision_incomplete_fields")
        reasons = sorted(set(reasons))
        reason = reasons[0] if reasons else None
        detail = {
            "evidence_id": op["evidence_id"],
            "kind": op["kind"],
            "replacement_import": target,
            "scope": scope,
            "status": "unresolved" if reason else "applied",
            "reason": reason,
            "reasons": reasons,
        }
        self.coverage.append(detail)
        if reason:
            # Preserve valid prior evidence. A failed correction cannot compete with it.
            for cell in new:
                cell["suppressed"] = reason
            return
        changed_keys = {
            self.key(cell) for cell in new if cell["state"] not in {"missing", "unreported"}
        }
        for cell in self.cells:
            if (
                cell["import_id"] in op["supersedes"]
                and _matches(cell, scope)
                and (op["kind"] == "complete_snapshot" or self.key(cell) in changed_keys)
            ):
                cell["suppressed"] = "superseded_in_declared_scope"
        # Values outside an operation's scope are still ordinary independent assertions.
        # A missing patch field does nothing; a blank/withdrawal is an explicit state.

    @staticmethod
    def key(cell: dict[str, Any]) -> str:
        fields = ("table", "package_id", "period", "date", "field", *CONTEXT[cell["table"]])
        if cell["table"] == "price_observation":
            fields += ("basis_evidence_scope",)
        if cell["field"] == "group":
            fields += ("group_namespace", "group_scope")
        return canonical_json({key: cell[key] for key in fields})

    def resolve(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for cell in self.cells:
            grouped[self.key(cell)].append(cell)
        resolved = []
        for key, cells in sorted(grouped.items()):
            selected = [
                cell
                for cell in cells
                if not cell.get("suppressed") and cell["state"] not in {"unreported", "missing"}
            ]
            rules = [
                rule
                for rule in self.config["source_preference"]["rules"]
                if selected and all(_matches(cell, rule["scope"]) for cell in selected)
            ]
            if len(rules) > 1:
                raise ValueError("Overlapping source-preference rules")
            preferred = selected
            preference = None
            if rules and self.evidence_available(
                rules[0]["evidence_delivery_id"], rules[0]["source_locator"]
            ):
                rule = rules[0]
                if all(cell["source"] in rule["sources"] for cell in selected):
                    best = min(rule["sources"].index(cell["source"]) for cell in selected)
                    preferred = [
                        cell for cell in selected if rule["sources"].index(cell["source"]) == best
                    ]
                    preference = rule
            blocked = any(cell.get("blocked") for cell in preferred)
            values = {
                canonical_json({"state": cell["state"], "value": cell["value"]})
                for cell in preferred
            }
            if blocked:
                state, value, reason = "excluded", None, "invalid_or_conflicting_assertion"
            elif len(values) > 1:
                state, value, reason = "conflict", None, "unresolved_material_conflict"
            elif not values:
                state, value, reason = "missing", None, "no_supported_value"
            else:
                chosen = json.loads(next(iter(values)))
                state, value, reason = chosen["state"], chosen["value"], None
            supports = (
                [
                    cell
                    for cell in selected
                    if not cell.get("blocked") and cell["state"] == state and cell["value"] == value
                ]
                if state not in {"conflict", "excluded", "missing"}
                else []
            )
            comparable = not any(cell.get("limited_comparability") for cell in preferred)
            if state == "present" and not comparable:
                reason = "unknown_price_basis"
            resolved.append(
                {
                    "key": json.loads(key),
                    "state": state,
                    "value": value,
                    "usable": state == "present" and comparable,
                    "reason": reason,
                    "supporting_observation_ids": sorted(
                        {cell["observation_id"] for cell in supports}
                    ),
                    "mapping_ids": sorted({cell["mapping_id"] for cell in supports}),
                    "source_preference": preference,
                    "assertions": sorted(
                        cells, key=lambda cell: (cell["observation_id"], cell["field"])
                    ),
                }
            )
        return resolved

    def run(self) -> dict[str, Any]:
        self.load()
        self.revisions()
        result = self.resolve()
        # Coverage lists every requested package/time/table even when no row was admitted.
        requested = []
        slots_by_key: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in result:
            for key in ("period", "date"):
                if item["key"][key] is not None:
                    slots_by_key[
                        (
                            item["key"]["package_id"],
                            item["key"]["table"],
                            key,
                            item["key"][key],
                        )
                    ].append(item)
        for package in self.config["scope"]["package_ids"]:
            for table in self.config["scope"]["tables"]:
                for kind, key in (("periods", "period"), ("dates", "date")):
                    for moment in self.config["scope"][kind]:
                        slots = slots_by_key.get((package, table, key, moment), [])
                        requested.append(
                            {
                                "package_id": package,
                                "table": table,
                                key: moment,
                                "usable_fields": sum(item["usable"] for item in slots),
                                "status": "some_supported_fields"
                                if any(item["usable"] for item in slots)
                                else "no_usable_evidence",
                                "market_completeness": "unestablished",
                            }
                        )
        self.now = datetime.now(UTC)
        return {
            "version": 1,
            "policy_version": POLICY,
            "produced_at": self.now.isoformat(),
            "result": result,
            "requested_coverage": requested,
            "coverage": self.coverage,
            "exclusions": self.exclusions,
            "availability_disclosures": self.disclosures,
            "availability_claim": (
                "reconstructed evidence; no demonstrated historical availability or performance"
                if self.research
                else "supported publication; does not establish pharmacy possession or a past run"
                if self.config["availability_policy"] == "publication"
                else "supported receipt; production time is recorded separately"
            ),
            "late": self.now > self.cutoff if self.config["mode"] == "live" else None,
            "inputs": {
                "deliveries": self.deliveries,
                "imports": self.imports,
                "mappings": self.mappings,
                "original_mappings": self.original_mappings,
                "observations": self.observations,
                "issues": self.issues,
            },
        }


def _catalogue_predicate(catalogue: list[dict[str, str]]) -> tuple[str, list[str]]:
    placeholders = ", ".join("(?, ?)" for _ in catalogue)
    parameters = [
        value for item in catalogue for value in (item["namespace"], item["canonical_identifier"])
    ]
    return (
        f"(identifier_namespace, canonical_identifier) IN (VALUES {placeholders})",
        parameters,
    )


class CatalogueSelector(Selector):
    """Same F3 policy, with explicitly bounded input retention and no revisions."""

    def __init__(self, store: Store, config: dict[str, Any]) -> None:
        super().__init__(store, config)
        self.catalogue = {
            (item["namespace"], item["canonical_identifier"])
            for item in config["candidate_catalogue"]
        }
        self.candidate_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.inventory: list[dict[str, Any]] = []
        self.mapping_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.identity_documents: dict[str, dict[str, Any]] = {}
        self.identity_indexes: dict[int, dict[tuple[str, ...], list[dict[str, Any]]]] = {}

    def load(self) -> None:
        for mapping_id in self.config["mapping_ids"]:
            mapping = serial(self.store.get_record("package_identifier", mapping_id))
            key = (mapping["namespace"], mapping["canonical_value"])
            if key not in self.catalogue:
                raise ValueError("Selected mapping is outside the candidate catalogue")
            self.mapping_index[key].append(mapping)
        predicate, parameters = _catalogue_predicate(self.config["candidate_catalogue"])
        for import_id in self.config["import_ids"]:
            for table in FIELDS:
                count_row = self.store._connection.execute(
                    f"SELECT count(*) FROM {table} WHERE import_id = ?", [import_id]
                ).fetchone()
                assert count_row is not None
                total = count_row[0]
                result = self.store._connection.execute(
                    f"SELECT * FROM {table} WHERE import_id = ? AND {predicate} "
                    "ORDER BY observation_id",
                    [import_id, *parameters],
                )
                columns = [column[0] for column in result.description]
                retained = [
                    serial(dict(zip(columns, row, strict=True))) for row in result.fetchall()
                ]
                self.candidate_rows[(import_id, table)] = retained
                self.inventory.append(
                    {
                        "import_id": import_id,
                        "table": table,
                        "total": total,
                        "retained": len(retained),
                        "omitted": total - len(retained),
                    }
                )
                for row in retained:
                    mapping_id = row["mapping_id"]
                    if mapping_id not in self.original_mappings:
                        original = serial(self.store.get_record("package_identifier", mapping_id))
                        self.original_mappings[mapping_id] = original
                        self.batch(original["evidence_import_id"])
        super().load()

    def verification(
        self,
        observation_ids: set[str] | None = None,
        mapping_ids: set[str] | None = None,
    ) -> None:
        super().verification(
            {
                row["observation_id"]
                for retained in self.candidate_rows.values()
                for row in retained
            },
            set(self.config["mapping_ids"]) | self.original_mappings.keys(),
        )

    def observation_rows(self, import_id: str, table: str) -> Iterable[dict[str, Any]]:
        return self.candidate_rows[(import_id, table)]

    def mapping_candidates(self, row: dict[str, Any]) -> Iterable[dict[str, Any]]:
        return self.mapping_index.get(
            (row["identifier_namespace"], row["canonical_identifier"]), []
        )

    def identity_document(self, delivery_id: str) -> dict[str, Any]:
        if delivery_id not in self.identity_documents:
            document = super().identity_document(delivery_id)
            index: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
            for entry in document["entries"]:
                key = (
                    entry["source_locator"],
                    entry["package_id"],
                    entry["canonical_identifier"],
                    entry["namespace"],
                )
                # Duplicate entries remain distinct and therefore ineligible.
                entry["supporting_observation_ids"] = set(entry["supporting_observation_ids"])
                index[key].append(entry)
            document["allowed_delivery_ids"] = set(document["allowed_delivery_ids"])
            self.identity_indexes[id(document)] = index
            self.identity_documents[delivery_id] = document
        return self.identity_documents[delivery_id]

    def identity_entries(
        self, document: dict[str, Any], mapping: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return self.identity_indexes[id(document)].get(
            (
                mapping["source_locator"],
                mapping["package_id"],
                mapping["canonical_value"],
                mapping["namespace"],
            ),
            [],
        )

    def run(self) -> dict[str, Any]:
        evidence = super().run()
        evidence["version"] = 2
        evidence["input_retention"] = {
            "kind": "candidate_catalogue",
            "catalogue_hash": digest({"candidate_catalogue": self.config["candidate_catalogue"]}),
            "inventory": self.inventory,
            "omitted_rows": (
                "Outside-catalogue rows are counted but not individually frozen or checked "
                "during readback. Original source bytes and complete independent reports "
                "remain pinned; catalogue membership establishes no package identity."
            ),
        }
        return evidence


def create_selection(store: Store, configuration: dict[str, Any]) -> dict[str, Any]:
    store._check_owner()
    config = _configuration(configuration)
    selection_id = identity("selection", config)
    existing = store._connection.execute(
        "SELECT 1 FROM frozen_selection WHERE selection_id = ?", [selection_id]
    ).fetchone()
    if existing:
        return inspect_selection(store, selection_id)
    selector = (
        CatalogueSelector(store, config) if config["version"] == 2 else Selector(store, config)
    )
    evidence = selector.run()
    with store.transaction():
        store._insert(
            "frozen_selection",
            {
                "selection_id": selection_id,
                "policy_version": POLICY,
                "configuration": canonical_json(config),
                "configuration_hash": digest(config),
                "evidence": canonical_json(evidence),
                "evidence_hash": digest(evidence),
                "produced_at": selector.now,
                "decision_cutoff": selector.cutoff,
                "availability_mode": config["mode"],
            },
        )
    return inspect_selection(store, selection_id)


def inspect_selection(store: Store, selection_id: str) -> dict[str, Any]:
    store._check_owner()
    result = store._connection.execute(
        "SELECT * FROM frozen_selection WHERE selection_id = ?", [str(UUID(selection_id))]
    )
    row = result.fetchone()
    if row is None:
        raise ValueError("Selection does not exist")
    record = serial(dict(zip([col[0] for col in result.description], row, strict=True)))
    config, evidence = json.loads(record["configuration"]), json.loads(record["evidence"])
    if (
        digest(config) != record["configuration_hash"]
        or digest(evidence) != record["evidence_hash"]
    ):
        raise FoundationError("Frozen selection integrity mismatch")
    if identity("selection", config) != record["selection_id"]:
        raise FoundationError("Frozen configuration identity mismatch")
    if (
        record["policy_version"] != config["policy_version"]
        or evidence["policy_version"] != config["policy_version"]
        or record["availability_mode"] != config["mode"]
        or datetime.fromisoformat(record["decision_cutoff"])
        != datetime.fromisoformat(config["cutoff"])
        or datetime.fromisoformat(record["produced_at"])
        != datetime.fromisoformat(evidence["produced_at"])
    ):
        raise FoundationError("Frozen execution metadata changed")
    for delivery_id, delivery in evidence["inputs"]["deliveries"].items():
        store.original_path(delivery_id)
        if serial(store.get_record("source_delivery", delivery_id)) != delivery:
            raise FoundationError("Frozen delivery provenance changed")
    for name, table in (
        ("imports", "import_batch"),
        ("mappings", "package_identifier"),
        ("original_mappings", "package_identifier"),
    ):
        for key, original in evidence["inputs"][name].items():
            if serial(store.get_record(table, key)) != original:
                raise FoundationError("Frozen interpretation provenance changed")
    if config["version"] == 2:
        _inspect_catalogue(store, config, evidence)
    else:
        for observation_id, original in evidence["inputs"]["observations"].items():
            table = original["table"]
            if table not in FIELDS:
                raise FoundationError("Invalid frozen observation table")
            result = store._connection.execute(
                f"SELECT * FROM {table} WHERE observation_id = ?", [observation_id]
            )
            row = result.fetchone()
            if (
                row is None
                or serial(dict(zip([col[0] for col in result.description], row, strict=True)))
                != original["record"]
            ):
                raise FoundationError("Frozen source assertion changed")
    for import_id, issues in evidence["inputs"]["issues"].items():
        if serial(store.issues(import_id)) != issues:
            raise FoundationError("Frozen import issues changed")
    return {**record, "configuration": config, "evidence": evidence}


def _inspect_catalogue(store: Store, config: dict[str, Any], evidence: dict[str, Any]) -> None:
    retention = evidence["input_retention"]
    if (
        evidence["version"] != 2
        or retention["kind"] != "candidate_catalogue"
        or retention["catalogue_hash"]
        != digest({"candidate_catalogue": config["candidate_catalogue"]})
    ):
        raise FoundationError("Frozen candidate catalogue changed")
    by_table: dict[str, dict[str, Any]] = defaultdict(dict)
    for observation_id, original in evidence["inputs"]["observations"].items():
        if original["table"] not in FIELDS:
            raise FoundationError("Invalid frozen observation table")
        by_table[original["table"]][observation_id] = original["record"]
    for table, expected in by_table.items():
        ids = sorted(expected)
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            marks = ",".join("?" for _ in batch)
            result = store._connection.execute(
                f"SELECT * FROM {table} WHERE observation_id IN ({marks})", batch
            )
            columns = [column[0] for column in result.description]
            actual = [serial(dict(zip(columns, row, strict=True))) for row in result.fetchall()]
            if len(actual) != len(batch) or any(
                row != expected[row["observation_id"]] for row in actual
            ):
                raise FoundationError("Frozen source assertion changed")
    predicate, parameters = _catalogue_predicate(config["candidate_catalogue"])
    inventory = []
    for import_id in config["import_ids"]:
        for table in FIELDS:
            counts = store._connection.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {predicate}) "
                f"FROM {table} WHERE import_id = ?",
                [*parameters, import_id],
            ).fetchone()
            assert counts is not None
            total, retained = counts
            inventory.append(
                {
                    "import_id": import_id,
                    "table": table,
                    "total": total,
                    "retained": retained,
                    "omitted": total - retained,
                }
            )
    if inventory != retention["inventory"]:
        raise FoundationError("Frozen candidate inventory changed")


def selection_summary(selection: dict[str, Any]) -> dict[str, Any]:
    evidence = selection["evidence"]
    return {
        "selection_id": selection["selection_id"],
        "configuration_hash": selection["configuration_hash"],
        "evidence_hash": selection["evidence_hash"],
        "produced_at": selection["produced_at"],
        "availability_mode": selection["availability_mode"],
        "availability_claim": evidence["availability_claim"],
        "late": evidence["late"],
        "fields": dict(sorted(Counter(item["state"] for item in evidence["result"]).items())),
        "usable_fields": sum(item["usable"] for item in evidence["result"]),
        "exclusion_reasons": dict(
            sorted(Counter(item["reason"] for item in evidence["exclusions"]).items())
        ),
        "availability_disclosures": len(evidence["availability_disclosures"]),
    }
