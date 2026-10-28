"""Frozen, compact FTP-primary selections over the existing foundation.

Only edition/member choices are persisted. Native values remain in the verified
typed dictionaries. Queries resolve a declared tariff snapshot, never the last
download. Reference data retains its native grain. This policy supports
reconstructed research only; it makes no historical availability claim.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .lms_registry_layout import LAYOUTS
from .records import canonical_json, digest, identity

if TYPE_CHECKING:
    from .store import Store

POLICY = "registry-canonical-v1"
NATIVE_PRICE_VALUE_POLICY = "lms-native-price-values-v1"
NO_FIXED_PRICE_FROM = "2025-01-06"
NO_FIXED_PRICE_TO = "2026-10-13"
NO_FIXED_PRICE_CODES = {
    "AP": "Udelukkende produktionsdyr, en udlevering",
    "BP": "Udelukkende produktionsdyr, flere udleveringer",
    "HF": "Håndkøb, ikke apoteksforbeholdt",
    "HP": "Udelukkende produktionsdyr, håndkøb",
    "HV": "Håndkøb, veterinær, ikke apoteksforbeholdt",
    "HX": "Håndkøb, ikke apoteksforbeholdt, begrænset",
    "PRMIX": "Kun til fodermøller efter veterinær ordination",
}
NOT_CALCULATED_DESCRIPTION = "AUP ikke beregnet, vet. frihandel"
KEYS = {
    1: ("drug_id",),
    2: ("package_number",),
    3: ("package_number",),
    4: ("package_number",),
    5: ("package_number",),
    7: ("package_number",),
    9: ("company_code",),
    10: ("drug_id", "change_date"),
    11: ("code",),
    12: ("atc",),
    13: ("code",),
    14: ("code",),
    15: ("unit_type", "code"),
    16: ("code",),
    17: ("code",),
    18: ("code",),
    19: ("code",),
    20: ("code",),
    21: ("drug_id",),
    22: ("code",),
    23: ("patient_type", "level_code"),
    24: ("drug_id", "package_number"),
    25: ("atc", "indication_code", "drug_id"),
    26: ("code",),
    27: ("drug_id", "dosage_code"),
    28: ("code",),
    29: ("recommendation_group", "drug_id", "package_number"),
    30: ("drug_id", "package_number", "substance_class", "substance_group", "substance"),
    31: ("drug_id", "package_number"),
    32: ("ordered_package", "substitute_package", "alternative_package"),
    33: ("ordered_package", "substitute_package", "alternative_package"),
    34: ("drug_id", "package_number", "indication_group"),
    35: ("indication_group",),
    38: ("drug_id", "package_number"),
}
PRICE_FIELDS = {
    "aip_ore": "aip",
    "registered_ore": "registered",
    "consumer_ore": "consumer",
    "reimbursement_ore": "reimbursement_basis",
    "hospital_slot_08": "hospital_delivery",
    "non_reimbursable_ore": "non_reimbursable_component",
}
LEGACY_PARSERS = {
    "medicine-prices-pdf-v1",
    "price-changes-rtf-v1",
    "price-changes-workbook-v1",
    "price-changes-workbook-v2",
    "introductions-rtf-v1",
    "introductions-workbook-v2",
    "introductions-workbook-v3",
    "discontinued-rtf-v1",
    "discontinued-workbook-v2",
}
LEGACY_PROOF_WITHOUT_CALENDAR = "f4-explicit-import-proof-without-calendar-v1"
LEGACY_PROOF_WITHOUT_CALENDAR_AUDITOR = {
    "source_audit.py": "a8e3270cec0c609d018cd7a2f64856f7e444f797edf9a54ba73885179dcbed21",
    "rtf_audit.py": "45f3634a1a337fbbd62272ca0efe854a2bf7dddc1798d63773c318e3f264c8da",
    "pdf_audit.py": "23c126b18b86df016dc325b79cacb8e7a9c8a7f9c7642f4905d7a3667b74ddcf",
}


if TYPE_CHECKING:
    from .native_reuse import _NativeAttempt


def _serial(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _rows(store: Store, sql: str, parameters: list[Any] | None = None) -> list[dict[str, Any]]:
    result = store._connection.execute(sql, parameters or [])
    names = [column[0] for column in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def _scalar(store: Store, sql: str, parameters: list[Any]) -> Any:
    row = store._connection.execute(sql, parameters).fetchone()
    if row is None:
        raise ValueError("Required frozen evidence row is absent")
    return row[0]


def _date(value: Any) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Explicit ISO business date required")
    return value


def native_price_value_contract() -> dict[str, Any]:
    """Versioned interpretation of native values, never a typed-row repair."""
    return {
        "version": NATIVE_PRICE_VALUE_POLICY,
        "native_only": True,
        "zero_value_unresolved": [
            "3.aip_ore",
            "3.registered_ore",
            "3.consumer_ore",
            "3.hospital_slot_08",
        ],
        "zero_reference_preserved": ["3.reimbursement_ore", "3.non_reimbursable_ore"],
        "named_state_scope": {
            "effective_from": NO_FIXED_PRICE_FROM,
            "effective_to": NO_FIXED_PRICE_TO,
            "phase": "final",
            "snapshot": "exact tariff start; same selected package and reference snapshot",
        },
        "consumer_no_fixed_price": dict(NO_FIXED_PRICE_CODES),
        "registered_not_calculated": {"V": NOT_CALCULATED_DESCRIPTION},
        "positive_context_disagreement": "price_context_conflict; retain amount",
        "zero_amount": "retain exact native zero; never infer missing, free or entitlement",
        "unit_and_ddd": "independent native reference quantities; no package-zero inference",
        "legacy": "frozen legacy values and resolution decisions unchanged",
        "sentinels": "no capacity, saturation, rounding or correction inference",
    }


def _value_policy_binding(config: dict[str, Any]) -> dict[str, str]:
    value = config.get("native_price_value_policy")
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "evidence_delivery_id", "evidence_sha256", "source_locator"}
        or value["version"] != NATIVE_PRICE_VALUE_POLICY
        or not isinstance(value["source_locator"], str)
        or not value["source_locator"].strip()
        or not isinstance(value["evidence_sha256"], str)
        or len(value["evidence_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in value["evidence_sha256"])
    ):
        raise ValueError("Explicit evidence-pinned native price value policy required")
    value["evidence_delivery_id"] = str(UUID(value["evidence_delivery_id"]))
    return {**value, "contract_sha256": digest(native_price_value_contract())}


def _value_policy_input(store: Store, config: dict[str, Any]) -> dict[str, str]:
    binding = _value_policy_binding(config)
    delivery = store.get_record("source_delivery", binding["evidence_delivery_id"])
    path = store.original_path(binding["evidence_delivery_id"])
    data = path.read_bytes()
    document = json.loads(data)
    if (
        delivery["content_hash"] != binding["evidence_sha256"]
        or hashlib.sha256(data).hexdigest() != binding["evidence_sha256"]
        or not isinstance(document, dict)
        or document.get("policy") != native_price_value_contract()
    ):
        raise ValueError("Native price value policy evidence differs from this read contract")
    return binding


def _configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    config = json.loads(canonical_json(configuration))
    allowed = {
        "version",
        "mode",
        "release_import_ids",
        "period_bindings",
        "semantic_profiles",
        "legacy_selection_ids",
        "auditor_version",
        "supply_imports",
        "supply_source_preference",
        "builder_provenance",
        "supplement_import_ids",
        "legacy_imports",
        "native_price_value_policy",
    }
    if set(config) - allowed or config.get("version") != POLICY:
        raise ValueError("Unsupported registry selection configuration")
    if config.get("mode", "reconstructed_research") != "reconstructed_research":
        raise ValueError("This policy does not establish historical availability")
    config["mode"] = "reconstructed_research"
    _value_policy_binding(config)
    config.setdefault("supply_imports", [])
    supply_ids = set()
    for item in config["supply_imports"]:
        if set(item) != {"import_id", "verification_delivery_id"}:
            raise ValueError("Supply imports require independent verification evidence")
        for key in item:
            item[key] = str(UUID(item[key]))
        if item["import_id"] in supply_ids:
            raise ValueError("Duplicate supply import")
        supply_ids.add(item["import_id"])
    config["supply_imports"].sort(key=lambda item: item["import_id"])
    config.setdefault("legacy_imports", [])
    legacy_ids = set()
    for item in config["legacy_imports"]:
        if set(item) != {"import_id", "verification_delivery_id", "context_evidence_delivery_ids"}:
            raise ValueError("Legacy evidence requires explicit import, proof and context pins")
        item["import_id"] = str(UUID(item["import_id"]))
        item["verification_delivery_id"] = str(UUID(item["verification_delivery_id"]))
        evidence = item["context_evidence_delivery_ids"]
        if not isinstance(evidence, list) or len(set(evidence)) != len(evidence):
            raise ValueError("Distinct legacy context evidence required")
        item["context_evidence_delivery_ids"] = sorted(str(UUID(eid)) for eid in evidence)
        if item["import_id"] in legacy_ids:
            raise ValueError("Duplicate legacy evidence import")
        legacy_ids.add(item["import_id"])
    config["legacy_imports"].sort(key=lambda item: item["import_id"])
    preference = config.setdefault("supply_source_preference", [])
    if len(set(preference)) != len(preference) or any(
        not isinstance(item, str) or not item for item in preference
    ):
        raise ValueError("Supply source preference must be an explicit distinct ordering")
    for key in ("release_import_ids", "legacy_selection_ids", "supplement_import_ids"):
        values = config.get(key, [])
        if not isinstance(values, list) or len(set(values)) != len(values):
            raise ValueError("Expected distinct pinned input IDs")
        config[key] = sorted(str(UUID(value)) for value in values)
    if not config["release_import_ids"] or not config.get("period_bindings"):
        raise ValueError("Pin releases and explicit tariff snapshot bindings")
    seen = set()
    for binding in config["period_bindings"]:
        permitted = {
            "period_start",
            "snapshot_date",
            "phase",
            "chosen_import_id",
            "choice_evidence_delivery_id",
            "source_locator",
        }
        if set(binding) - permitted:
            raise ValueError("Unknown period binding field")
        key = (_date(binding["period_start"]), binding["phase"])
        if _date(binding["snapshot_date"]) != key[0]:
            raise ValueError("Canonical tariff snapshots require the exact official period start")
        if key in seen or key[1] not in {"final", "preliminary"}:
            raise ValueError("Duplicate or invalid period binding")
        seen.add(key)
        if binding.get("chosen_import_id"):
            binding["chosen_import_id"] = str(UUID(binding["chosen_import_id"]))
            if binding["chosen_import_id"] not in config["release_import_ids"]:
                raise ValueError("Chosen edition is not pinned")
            if not binding.get("choice_evidence_delivery_id") or not binding.get("source_locator"):
                raise ValueError("Edition precedence requires explicit located evidence")
    config["period_bindings"].sort(key=lambda row: (row["period_start"], row["phase"]))
    if not isinstance(config.get("semantic_profiles"), list) or not config["semantic_profiles"]:
        raise ValueError("Reviewed field semantics required")
    for rule in config["semantic_profiles"]:
        permitted = {
            "profile",
            "effective_from",
            "effective_to",
            "fields",
            "complete_snapshot_members",
            "evidence_delivery_id",
            "source_locator",
            "monetary_context",
        }
        if set(rule) - permitted or not rule.get("source_locator") or not rule.get("profile"):
            raise ValueError("Invalid semantic profile")
        rule["evidence_delivery_id"] = str(UUID(rule["evidence_delivery_id"]))
        if _date(rule["effective_from"]) >= _date(rule["effective_to"]):
            raise ValueError("Semantic profile interval must be half-open")
        for member, fields in rule["fields"].items():
            number = int(member)
            known = {field[0] for field in LAYOUTS.get(number, (0, ()))[1]}
            if str(number) != member or not set(fields) <= known or len(fields) != len(set(fields)):
                raise ValueError("Unsupported semantic field")
            if any(field.startswith(("legacy_", "reserved_")) for field in fields):
                raise ValueError("Reserved and historical opaque slots have no canonical semantics")
            rule["fields"][member] = sorted(fields)
        complete = rule.get("complete_snapshot_members", [])
        if not set(complete) <= {int(key) for key in rule["fields"]}:
            raise ValueError("Complete replacement scope lacks supported fields")
        rule["complete_snapshot_members"] = sorted(set(complete))
        # Monetary contexts are per member.field, never one basis for the release.
        for field, context in rule.get("monetary_context", {}).items():
            member, name = field.split(".", 1)
            if name not in rule["fields"].get(member, []):
                raise ValueError("Monetary context outside supported field scope")
            if set(context) != {"currency", "tax_basis", "fee_basis", "price_basis", "price_role"}:
                raise ValueError("Complete per-field monetary context required")
            if any(not isinstance(value, str) or not value for value in context.values()):
                raise ValueError("Invalid monetary context")
        rule.setdefault("monetary_context", {})
    return config


def build_configuration(
    store: Store, policy: dict[str, Any], *, destination: Path | None = None
) -> dict[str, Any]:
    """Resolve a reviewed maintenance policy to an immutable explicit selection.

    ``latest_verified`` limits effective-date coverage, not source availability.
    Every known official period inside that range is retained, including gaps.
    The builder never selects a correction winner or extends semantic profiles.
    """
    from urllib.parse import urlsplit

    from .archive import external_path

    store._check_owner()
    policy = json.loads(canonical_json(policy))
    allowed = {
        "version",
        "history_from",
        "history_through",
        "sources",
        "source_role",
        "excluded_source_areas",
        "phases",
        "semantic_profiles",
        "legacy_selection_ids",
        "supply_imports",
        "supply_source_preference",
        "auditor_version",
        "legacy_imports",
        "native_price_value_policy",
    }
    if set(policy) - allowed or policy.get("version") != "registry-canonical-policy-v1":
        raise ValueError("Unsupported canonical maintenance policy")
    beginning = _date(policy["history_from"])
    ending = policy["history_through"]
    if ending != "latest_verified":
        ending = _date(ending)
        if ending < beginning:
            raise ValueError("Reversed historical coverage interval")
    sources = policy.get("sources")
    phases = policy.get("phases", ["final"])
    if (
        not isinstance(sources, list)
        or not sources
        or any(not isinstance(x, str) or not x for x in sources)
    ):
        raise ValueError("Explicit production source names required")
    if not policy.get("source_role") or not phases or not set(phases) <= {"final", "preliminary"}:
        raise ValueError("Explicit production role and phase required")
    areas = policy.get("excluded_source_areas", [])
    if any(not isinstance(area, str) or not area.startswith("/") for area in areas):
        raise ValueError("Excluded source areas must be absolute source directory prefixes")
    rows = _rows(
        store,
        "SELECT r.import_id,r.effective_date,r.phase,r.profile,r.context,d.source,d.original_uri "
        "FROM registry_release r JOIN import_batch i USING(import_id) "
        "JOIN source_delivery d ON d.delivery_id=r.delivery_id "
        "WHERE i.validation_status='accepted' ORDER BY r.effective_date,r.import_id",
    )
    selected: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for row in rows:
        moment = row["effective_date"].isoformat()
        context = json.loads(row["context"])
        remote = context.get("remote") or urlsplit(row["original_uri"] or "").path
        reason = None
        if row["source"] not in sources or context.get("source_role") != policy["source_role"]:
            reason = "outside_production_source_role"
        elif row["phase"] not in phases:
            reason = "outside_release_phase"
        elif any(
            remote.casefold() == area.rstrip("/").casefold()
            or remote.casefold().startswith(area.rstrip("/").casefold() + "/")
            for area in areas
        ):
            reason = "excluded_source_area"
        elif moment < beginning or (ending != "latest_verified" and moment > ending):
            reason = "outside_effective_coverage"
        else:
            proofs = _rows(
                store,
                "SELECT auditor_version FROM registry_release_verification WHERE import_id=?",
                [row["import_id"]],
            )
            if not any(
                policy.get("auditor_version") in (None, proof["auditor_version"])
                for proof in proofs
            ):
                reason = "not_independently_verified"
        if reason:
            excluded[reason] += 1
        else:
            selected.append(row)
    if not selected:
        raise ValueError("No verified registry releases match the reviewed production policy")
    if ending == "latest_verified":
        ending = max(row["effective_date"].isoformat() for row in selected)
    calendar = _rows(
        store,
        "SELECT period_start,period_end_exclusive FROM price_period "
        "WHERE period_start<=? AND period_end_exclusive>? ORDER BY period_start",
        [ending, beginning],
    )
    if any(
        not any(
            period["period_start"] <= moment < period["period_end_exclusive"] for period in calendar
        )
        for moment in {row["effective_date"] for row in selected}
    ):
        raise ValueError("Verified registry dates lack reviewed tariff-calendar coverage")
    supplements = []
    supplement_excluded: Counter[str] = Counter()
    for row in _rows(
        store,
        "SELECT s.import_id,s.kind,s.metadata,s.verification,d.source,d.original_uri "
        "FROM supplement_import s JOIN import_batch i USING(import_id) "
        "JOIN source_delivery d ON d.delivery_id=s.delivery_id "
        "WHERE i.validation_status='accepted' ORDER BY s.import_id",
    ):
        remote = urlsplit(row["original_uri"] or "").path
        metadata, proof = json.loads(row["metadata"]), json.loads(row["verification"])
        reason = None
        if row["source"] not in sources:
            reason = "outside_production_source"
        elif any(
            remote.casefold() == area.rstrip("/").casefold()
            or remote.casefold().startswith(area.rstrip("/").casefold() + "/")
            for area in areas
        ):
            reason = "excluded_source_area"
        elif not proof.get("accepted"):
            reason = "not_independently_verified"
        elif row["kind"] == "DOK" and not (
            metadata.get("effective_date") and beginning <= metadata["effective_date"] <= ending
        ):
            reason = "outside_native_effective_coverage"
        if reason:
            supplement_excluded[reason] += 1
        else:
            supplements.append(str(row["import_id"]))
    periods = _rows(
        store,
        "SELECT period_start FROM price_period WHERE period_start>=? AND period_start<=? "
        "ORDER BY period_start",
        [beginning, ending],
    )
    config = {
        "version": POLICY,
        "mode": "reconstructed_research",
        "release_import_ids": [str(row["import_id"]) for row in selected],
        "period_bindings": [
            {
                "period_start": row["period_start"].isoformat(),
                "snapshot_date": row["period_start"].isoformat(),
                "phase": phase,
            }
            for row in periods
            for phase in phases
        ],
        "semantic_profiles": policy["semantic_profiles"],
        "supplement_import_ids": supplements,
        "builder_provenance": {
            "policy_hash": digest(policy),
            "policy": policy,
            "resolved_history_through": ending,
            "excluded_import_counts": dict(sorted(excluded.items())),
            "excluded_supplement_counts": dict(sorted(supplement_excluded.items())),
        },
    }
    for key in (
        "legacy_selection_ids",
        "supply_imports",
        "supply_source_preference",
        "auditor_version",
        "legacy_imports",
        "native_price_value_policy",
    ):
        if key in policy:
            config[key] = policy[key]
    config = _configuration(config)
    if destination is not None:
        destination = external_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(config) + "\n")
    return config


def _input(
    store: Store, import_id: str, auditor: str | None, *, verify_original: bool = True
) -> dict[str, Any]:
    from .lms_registry import FIXED_WIDTH_PROFILE, MISSING_FINAL_CRLF_PROFILE
    from .lms_registry_layout import PROFILE

    releases = _rows(store, "SELECT * FROM registry_release WHERE import_id=?", [import_id])
    if not releases:
        raise ValueError("Pinned registry release does not exist")
    release = releases[0]
    batch = store.get_record("import_batch", import_id)
    if batch["validation_status"] != "accepted":
        raise ValueError("Registry input is not accepted")
    proofs = _rows(
        store,
        "SELECT * FROM registry_release_verification WHERE import_id=? ORDER BY auditor_version",
        [import_id],
    )
    proofs = [proof for proof in proofs if auditor is None or proof["auditor_version"] == auditor]
    if not proofs:
        raise ValueError("Independent native reconciliation required")
    proof = proofs[0]
    report = json.loads(proof["report"])
    if (
        not report.get("complete")
        or digest(report) != proof["evidence_hash"]
        or report.get("import_id") != import_id
        or report.get("auditor") != proof["auditor_version"]
    ):
        raise ValueError("Invalid native reconciliation proof")
    context = json.loads(batch["interpretation_context"])
    import_key = {
        key: batch[key]
        for key in (
            "parser_version",
            "schema_version",
            "mapping_version",
            "interpretation_context_hash",
        )
    }
    import_key["delivery_id"] = str(batch["delivery_id"])
    native = report["native"]
    if (
        str(batch["delivery_id"]) != str(release["delivery_id"])
        or batch["parser_version"] not in {PROFILE, MISSING_FINAL_CRLF_PROFILE, FIXED_WIDTH_PROFILE}
        or batch["schema_version"] != "registry-1"
        or batch["mapping_version"] != "edition-local-native-keys-v1"
        or digest(context) != batch["interpretation_context_hash"]
        or identity("import", import_key) != import_id
        or context != {**json.loads(release["context"]), "phase": release["phase"]}
        or release["profile"] != batch["parser_version"] + ":system-" + release["system_version"]
        or (
            batch["parser_version"] == MISSING_FINAL_CRLF_PROFILE
            and native.get("unterminated_members") != ["lms02.txt"]
        )
        or (
            batch["parser_version"] == FIXED_WIDTH_PROFILE and not native.get("fixed_width_members")
        )
        or native["phase"] != release["phase"]
        or native["schema_version"] != release["system_version"]
        or native["effective_token"] != release["effective_date"].strftime("%Y%m%d")
        or native["period_week_token"] != release["tariff_week"]
        or native["parameter_record"] != release["system_parameters"]
    ):
        raise ValueError("Verified registry source context changed")
    bindings = _rows(
        store,
        "SELECT b.member_number,b.member_id,b.native_name,m.profile,"
        "m.raw_sha256,m.row_count,m.ordering_hash FROM registry_release_member b "
        "JOIN registry_member m USING(member_id) WHERE b.import_id=? "
        "ORDER BY member_number",
        [import_id],
    )
    expected = {item["member_number"]: item for item in report["members"]}
    if {row["member_number"] for row in bindings} != set(expected):
        raise ValueError("Verified release member inventory changed")
    if {row["native_name"].lower() for row in bindings} != set(native["members"]) - {"system.txt"}:
        raise ValueError("Verified native source locators changed")
    for row in bindings:
        item = expected[row["member_number"]]
        native_member = native["members"][row["native_name"].lower()]
        if (
            any(row[key] != item[key] for key in ("member_id", "raw_sha256", "ordering_hash"))
            or row["row_count"] != item["source_records"]
            or row["profile"] != PROFILE + ":system-" + release["system_version"]
            or native_member["sha256"] != row["raw_sha256"]
            or native_member["physical_records"] != row["row_count"]
        ):
            raise ValueError("Verified member binding changed")
        member_proof = store._connection.execute(
            "SELECT typed_sha256,original_sha256 FROM registry_member_verification "
            "WHERE member_id=? AND auditor_version=?",
            [row["member_id"], proof["auditor_version"]],
        ).fetchone()
        if member_proof != (item["typed_sha256"], row["raw_sha256"]):
            raise ValueError("Verified typed member proof changed")
    expected_count = sum(row["row_count"] for row in bindings)
    if (batch["record_count"], batch["accepted_count"], batch["rejected_count"]) != (
        expected_count,
        expected_count,
        0,
    ):
        raise ValueError("Verified registry import inventory changed")
    delivery = store.get_record("source_delivery", str(release["delivery_id"]))
    if verify_original:
        store.original_path(str(release["delivery_id"]))
    pinned = _serial(
        {
            "release": release,
            "batch": batch,
            "delivery": delivery,
            "members": bindings,
            "auditor_version": proof["auditor_version"],
            "evidence_hash": proof["evidence_hash"],
        }
    )
    return {**pinned, "input_hash": digest(pinned)}


def _rule(config: dict[str, Any], release: dict[str, Any]) -> dict[str, Any] | None:
    matched = [
        rule
        for rule in config["semantic_profiles"]
        if rule["profile"] == release["profile"]
        and rule["effective_from"] <= release["effective_date"] < rule["effective_to"]
    ]
    if len(matched) > 1:
        raise ValueError("Overlapping semantic profiles")
    return matched[0] if matched else None


def _supply_input(store: Store, item: dict[str, str]) -> dict[str, Any]:
    """Check persisted positive reports against their existing independent proofs."""
    from .selection import record_hash

    iid = item["import_id"]
    batch = store.get_record("import_batch", iid)
    if batch["validation_status"] != "accepted" or batch["parser_version"] != "supply-workbook-v1":
        raise ValueError("Only verified supply imports enter the supplementary supply domain")
    delivery = store.get_record("source_delivery", str(batch["delivery_id"]))
    store.original_path(str(batch["delivery_id"]))
    proof_delivery = store.get_record("source_delivery", item["verification_delivery_id"])
    raw = store.original_path(item["verification_delivery_id"]).read_bytes()
    report = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
    if (
        report.get("version") != 1
        or report.get("complete") is not True
        or report.get("mismatches")
        or report.get("quarantined_observation_ids")
    ):
        raise ValueError("Incomplete independent supply verification")
    proofs = [proof for proof in report["imports"] if proof["import_id"] == iid]
    if (
        len(proofs) != 1
        or proofs[0].get("status") != "verified"
        or proofs[0].get("mismatch_count") != 0
        or proofs[0].get("import_record_hash") != record_hash(batch)
    ):
        raise ValueError("Supply import does not match independent verification")
    hashes = report["observation_hashes"]
    row_digest = hashlib.sha256()
    cursor = store._connection.execute(
        "SELECT * FROM supply_observation WHERE import_id=? ORDER BY observation_id", [iid]
    )
    names = [column[0] for column in cursor.description]
    count = 0
    while rows := cursor.fetchmany(4096):
        for values in rows:
            row = dict(zip(names, values, strict=True))
            actual = record_hash(row)
            if hashes.get(str(row["observation_id"])) != actual:
                raise ValueError("Supply assertion does not match independent verification")
            row_digest.update(actual.encode("ascii"))
            count += 1
    if not count:
        raise ValueError("Pinned supply import has no observations")
    # Verify complete assertion inventory, not merely the surviving supply subset.
    actual_assertions = sum(
        _scalar(store, f"SELECT count(*) FROM {table} WHERE import_id=?", [iid])
        for table in (
            "price_observation",
            "package_observation",
            "classification_observation",
            "supply_observation",
            "lifecycle_event",
        )
    )
    if actual_assertions != proofs[0].get("actual_assertions"):
        raise ValueError("Supply import assertion inventory changed")
    return {
        "import_id": iid,
        "verification_delivery_id": item["verification_delivery_id"],
        "input_hash": digest(
            _serial({"batch": batch, "delivery": delivery, "proof_delivery": proof_delivery})
        ),
        "rows_hash": row_digest.hexdigest(),
        "row_count": count,
        "content_hash": delivery["content_hash"],
        "source": delivery["source"],
    }


def _legacy_input(store: Store, item: dict[str, Any]) -> dict[str, Any]:
    """Reconcile existing F4 rows to accepted proofs, bounded to one import.

    Native originals are hash-checked but never reparsed here. Individual row
    hashes bind the observation inventory; audited logical snapshots bind source
    metadata, issues, mappings and context. No longitudinal identity is inferred.
    """
    from .ingestion import TABLE_FIELDS
    from .selection import record_hash
    from .source_audit import canonical as audit_canonical
    from .source_audit import serial as audit_serial

    iid = item["import_id"]
    batch = store.get_record("import_batch", iid)
    if batch["validation_status"] != "accepted" or batch["parser_version"] not in LEGACY_PARSERS:
        raise ValueError("Unsupported legacy evidence interpretation")
    delivery_id = str(batch["delivery_id"])
    delivery = store.get_record("source_delivery", delivery_id)
    store.original_path(delivery_id)
    proof_id = item["verification_delivery_id"]
    proof_delivery = store.get_record("source_delivery", proof_id)
    raw = store.original_path(proof_id).read_bytes()
    report = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
    if (
        report.get("version") != 1
        or report.get("complete") is not True
        or report.get("mismatches") != []
        or report.get("quarantined_observation_ids") != []
        or report.get("audit_scope") != {"kind": "explicit_imports", "import_ids": [iid]}
        or len(report.get("imports", [])) != 1
    ):
        raise ValueError("Legacy evidence requires a complete independently scoped proof")
    metadata_tables = {
        "import_batch",
        "import_issue",
        "source_delivery",
        "package_identifier",
        "package",
    }
    baseline = report.get("baseline_tables")
    historical_tables = metadata_tables | set(TABLE_FIELDS)
    without_calendar = (
        isinstance(baseline, dict)
        and set(baseline) == historical_tables
        and report.get("auditor_code") == LEGACY_PROOF_WITHOUT_CALENDAR_AUDITOR
    )
    if not isinstance(baseline, dict) or (
        set(baseline) != historical_tables | {"price_period"} and not without_calendar
    ):
        raise ValueError("Unsupported legacy proof baseline scope or producer")
    proof = report["imports"][0]
    if (
        proof.get("import_id") != iid
        or proof.get("delivery_id") != delivery_id
        or proof.get("parser_version") != batch["parser_version"]
        or proof.get("status") != "verified"
        or proof.get("mismatch_count") != 0
        or proof.get("import_record_hash") != record_hash(batch)
        or proof.get("expected_assertions") != proof.get("actual_assertions")
        or proof.get("expected_issues") != proof.get("actual_issues")
    ):
        raise ValueError("Legacy import does not match its accepted proof")
    hashes, verified = report["observation_hashes"], report["verified_observation_ids"]
    if len(set(verified)) != len(verified) or set(verified) != set(hashes):
        raise ValueError("Legacy proof assertion inventory is inconsistent")
    observed: set[str] = set()
    referenced_periods = {
        delivery[key] for key in ("current_period", "target_period") if delivery[key] is not None
    }
    actual_tables = {}
    for table in TABLE_FIELDS:
        sha = hashlib.sha256()
        cursor = store._connection.execute(
            f"SELECT * FROM {table} WHERE import_id=? ORDER BY observation_id", [iid]
        )
        names = [column[0] for column in cursor.description]
        count = 0
        while records := cursor.fetchmany(4096):
            for values in records:
                row = dict(zip(names, values, strict=True))
                oid = str(row["observation_id"])
                actual = record_hash(row)
                if hashes.get(oid) != actual or str(row["delivery_id"]) != delivery_id:
                    raise ValueError("Legacy assertion does not match independent verification")
                observed.add(oid)
                referenced_periods.update(
                    row[key]
                    for key in ("effective_period", "report_period")
                    if row[key] is not None
                )
                sha.update(actual.encode("ascii"))
                count += 1
        if count != report["baseline_tables"][table]["count"]:
            raise ValueError("Legacy table assertion count changed")
        actual_tables[table] = {"count": count, "rows_hash": sha.hexdigest()}
    if (
        observed != set(verified)
        or len(observed) != proof["actual_assertions"]
        or len(observed) != batch["accepted_count"]
        or actual_tables["supply_observation"]["count"]
    ):
        raise ValueError("Legacy inventory changed or supply requires its dedicated domain")
    context = json.loads(batch["interpretation_context"]).get("adapter_context", {})
    override = context.get("price_basis_override", {})
    required_evidence = {override["evidence_delivery_id"]} if override else set()
    declared = set(item["context_evidence_delivery_ids"])
    if not required_evidence <= declared or (
        context.get("tax_basis", "unknown") != "unknown" and not declared
    ):
        raise ValueError("Legacy monetary definition dependencies must be pinned")
    context_records = {eid: store.get_record("source_delivery", eid) for eid in sorted(declared)}
    for eid in declared:
        store.original_path(eid)
    if (
        override
        and context_records[override["evidence_delivery_id"]]["content_hash"]
        != override["evidence_sha256"]
    ):
        raise ValueError("Legacy monetary definition bytes disagree with the import")
    filters: dict[str, tuple[str, list[Any]]] = {
        "import_batch": ("import_id=?", [iid]),
        "import_issue": ("import_id=?", [iid]),
        "package_identifier": ("evidence_import_id=?", [iid]),
        "package": (
            "package_id IN (SELECT package_id FROM package_identifier WHERE evidence_import_id=?)",
            [iid],
        ),
        "price_period": ("period_start=?", [delivery["current_period"]]),
        "source_delivery": (
            "delivery_id IN (SELECT unnest(?::UUID[]))",
            [[delivery_id, *sorted(required_evidence - {delivery_id})]],
        ),
    }
    if without_calendar:
        # The known historical producer never included price_period in its
        # explicit-import snapshot. Do not invent an audited empty baseline.
        del filters["price_period"]
    for table, (condition, parameters) in filters.items():
        metadata_rows = _rows(store, f"SELECT * FROM {table} WHERE {condition}", parameters)
        encoded = sorted((audit_serial(row) for row in metadata_rows), key=audit_canonical)
        actual_baseline = {
            "count": len(encoded),
            "sha256": hashlib.sha256(audit_canonical(encoded).encode()).hexdigest(),
        }
        if actual_baseline != report["baseline_tables"].get(table):
            raise ValueError("Legacy audited metadata, mapping, issues or calendar changed")
    current_calendar = {}
    if without_calendar:
        calendar_rows = _rows(
            store,
            "SELECT * FROM price_period WHERE period_start IN "
            "(SELECT unnest(?::DATE[])) ORDER BY period_start",
            [sorted(referenced_periods)],
        )
        if {row["period_start"] for row in calendar_rows} != referenced_periods:
            raise ValueError("Referenced legacy calendar period is missing")
        portable_calendar = [audit_serial(row) for row in calendar_rows]
        current_calendar = {
            "proof_profile": LEGACY_PROOF_WITHOUT_CALENDAR,
            "calendar_binding": {
                "original_proof_scope": "not_audited",
                "binding_scope": "current_referenced_calendar_rows_at_publication",
                "referenced_periods": [period.isoformat() for period in sorted(referenced_periods)],
                "rows": portable_calendar,
                "sha256": hashlib.sha256(audit_canonical(portable_calendar).encode()).hexdigest(),
            },
        }
    return {
        "import_id": iid,
        "delivery_id": delivery_id,
        "verification_delivery_id": proof_id,
        "proof_hash": proof_delivery["content_hash"],
        "input_hash": digest(
            _serial(
                {
                    "batch": batch,
                    "delivery": delivery,
                    "proof_delivery": proof_delivery,
                    "context_evidence": context_records,
                    **current_calendar,
                }
            )
        ),
        "tables": actual_tables,
        "audited_baseline_hash": digest(report["baseline_tables"]),
        **current_calendar,
    }


def _supplement_input(store: Store, import_id: str) -> dict[str, Any]:
    """Pin and independently reconcile every persisted located supplement row."""
    from .ftp_supplement_store import AUDITOR_VERSION, supplement_records
    from .ftp_supplements import VERSION, independent_audit, parse_supplement

    rows = _rows(store, "SELECT * FROM supplement_import WHERE import_id=?", [import_id])
    if not rows:
        raise ValueError("Unknown supplement import")
    native = rows[0]
    batch = store.get_record("import_batch", import_id)
    delivery_id = str(native["delivery_id"])
    if (
        batch["validation_status"] != "accepted"
        or batch["parser_version"] != VERSION
        or str(batch["delivery_id"]) != delivery_id
    ):
        raise ValueError("Supplement input must be an accepted native interpretation")
    delivery = store.get_record("source_delivery", delivery_id)
    data = store.original_path(delivery_id).read_bytes()
    records = supplement_records(store, import_id)
    proof = {
        "auditor_version": AUDITOR_VERSION,
        "original_sha256": hashlib.sha256(data).hexdigest(),
        "typed_sha256": digest({"records": records}),
        **independent_audit(data, native["kind"], records),
    }
    if proof != json.loads(native["verification"]) or not proof["accepted"]:
        raise ValueError("Persisted supplement values fail independent native verification")
    parsed = parse_supplement(data, native["kind"])
    if (
        parsed["metadata"] != json.loads(native["metadata"])
        or parsed["coverage"] != json.loads(native["coverage"])
        or native["original_sha256"] != proof["original_sha256"]
        or delivery["content_hash"] != proof["original_sha256"]
        or len(data) != delivery["byte_count"]
        or (batch["record_count"], batch["accepted_count"], batch["rejected_count"])
        != (len(records), len(records), 0)
    ):
        raise ValueError("Supplement provenance or complete coverage changed")
    return {
        "import_id": import_id,
        "kind": native["kind"],
        "input_hash": digest(_serial({"native": native, "batch": batch, "delivery": delivery})),
        "original_sha256": proof["original_sha256"],
        "proof_hash": digest(proof),
        "rows_hash": proof["typed_sha256"],
        "row_count": len(records),
    }


def _verify_typed(store: Store, inputs: dict[str, dict[str, Any]]) -> None:
    """Rehash stored values against independent proofs once per unique member.

    The auditor's canonical bytes are sorted ASCII JSON plus LF; this is a hash
    serialization, not a second interpretation of the native source.
    """
    checked: dict[int, str] = {}
    for item in inputs.values():
        for member in item["members"]:
            mid, number = member["member_id"], member["member_number"]
            expected = _scalar(
                store,
                "SELECT typed_sha256 FROM registry_member_verification "
                "WHERE member_id=? AND auditor_version=?",
                [mid, item["auditor_version"]],
            )
            if mid in checked:
                if checked[mid] != expected:
                    raise ValueError("Independent typed member proofs disagree")
                continue
            native_member = _rows(
                store,
                "SELECT row_ids,ordering_hash,row_count FROM registry_member WHERE member_id=?",
                [mid],
            )[0]
            ids, ordering, count = (
                native_member[key] for key in ("row_ids", "ordering_hash", "row_count")
            )
            if len(ids) != count or digest({"row_ids": ids}) != ordering:
                raise ValueError("Verified native occurrence ordering changed")
            cursor = store._connection.execute(
                f"SELECT r.* FROM unnest(?::BIGINT[]) WITH ORDINALITY u(row_id,ordinal) "
                f"JOIN registry_lms{number:02} r USING(row_id) ORDER BY u.ordinal",
                [ids],
            )
            names = [column[0] for column in cursor.description]
            sha, actual_count = hashlib.sha256(), 0
            while batch := cursor.fetchmany(4096):
                for values in batch:
                    row = dict(zip(names, values, strict=True))
                    if row.pop("profile") != member["profile"]:
                        raise ValueError("Native row semantic profile changed")
                    row.pop("row_id")
                    row["field_states"] = json.loads(row["field_states"])
                    sha.update(
                        (
                            json.dumps(
                                _serial(row),
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                            )
                            + "\n"
                        ).encode()
                    )
                    actual_count += 1
            if actual_count != count or sha.hexdigest() != expected:
                raise ValueError("Persisted typed registry values changed")
            checked[mid] = expected


def publish(
    store: Store,
    configuration: dict[str, Any],
    *,
    trusted_verification: bool = False,
    _native_reuse: _NativeAttempt | None = None,
) -> dict[str, Any]:
    """Freeze reviewed choices transactionally; retries do not add dataset rows."""
    store._check_owner()
    config = _configuration(configuration)
    if _native_reuse is not None:
        from .native_reuse import _NativeAttempt

        if not isinstance(_native_reuse, _NativeAttempt) or trusted_verification:
            raise ValueError("Opaque native proof attempt required")
        _native_reuse.start(store, config)
    value_policy = _value_policy_input(store, config)
    config_hash = digest(config)
    dataset_id = identity("analytical-dataset", {"configuration_hash": config_hash})
    if store._connection.execute(
        "SELECT 1 FROM analytical_dataset WHERE dataset_id=?", [dataset_id]
    ).fetchone():
        return status(store, dataset_id, verify=True, _native_reuse=_native_reuse)
    inputs = {
        iid: _input(
            store,
            iid,
            config.get("auditor_version"),
            verify_original=not trusted_verification and _native_reuse is None,
        )
        for iid in config["release_import_ids"]
    }
    evidence_ids = {rule["evidence_delivery_id"] for rule in config["semantic_profiles"]}
    evidence_ids.add(value_policy["evidence_delivery_id"])
    evidence_ids.update(
        binding["choice_evidence_delivery_id"]
        for binding in config["period_bindings"]
        if binding.get("chosen_import_id")
    )
    evidence = {
        eid: digest(_serial(store.get_record("source_delivery", eid)))
        for eid in sorted(evidence_ids)
    }
    for eid in evidence:
        store.original_path(eid)
    by_date: dict[tuple[str, str], list[str]] = defaultdict(list)
    for iid, item in inputs.items():
        release = item["release"]
        by_date[(release["effective_date"], release["phase"])].append(iid)
    periods, choices = [], []
    for binding in config["period_bindings"]:
        start, snapshot, phase = (
            binding[key] for key in ("period_start", "snapshot_date", "phase")
        )
        store.get_record("price_period", date.fromisoformat(start))
        ordinal = _scalar(store, "SELECT count(*) FROM price_period WHERE period_start<=?", [start])
        periods.append([dataset_id, start, snapshot, phase, ordinal])
        candidates = sorted(by_date.get((snapshot, phase), []))
        chosen = binding.get("chosen_import_id")
        if chosen and chosen not in candidates:
            raise ValueError("Chosen release does not match snapshot date and phase")
        admitted = [chosen] if chosen else candidates
        for number in sorted(LAYOUTS):
            offered = []
            for iid in admitted:
                item = inputs[iid]
                rule = _rule(config, item["release"])
                member = next(
                    (row for row in item["members"] if row["member_number"] == number), None
                )
                if rule and rule["fields"].get(str(number)):
                    offered.append((iid, member, rule))
            fields = (
                sorted(set.intersection(*(set(row[2]["fields"][str(number)]) for row in offered)))
                if offered
                else []
            )
            complete = bool(offered) and all(
                number in row[2]["complete_snapshot_members"] for row in offered
            )
            contexts = {canonical_json(row[2]["monetary_context"]) for row in offered}
            reason: str | None
            state, reason, member_id, selected, supports = (
                "missing",
                "no_exact_snapshot",
                None,
                None,
                [],
            )
            if candidates and not offered:
                state, reason = "unsupported", "no_supported_semantic_profile"
            elif offered:
                signatures = {
                    (row[1]["profile"], row[1]["raw_sha256"]) if row[1] else None for row in offered
                }
                if len(offered) != len(admitted) or not fields or len(contexts) != 1:
                    state, reason = "unsupported", "incomparable_semantic_profiles"
                elif len(signatures) > 1:
                    state, reason = "conflict", "unresolved_edition_conflict"
                elif None in signatures:
                    state, reason = "missing", "member_not_reported"
                else:
                    state, reason = "selected", None
                    selected, member, _ = offered[0]
                    assert member is not None
                    member_id = member["member_id"]
                    supports = [row[0] for row in offered]
            choices.append(
                [
                    dataset_id,
                    start,
                    phase,
                    number,
                    state,
                    member_id,
                    selected,
                    supports,
                    candidates,
                    fields,
                    complete,
                    next(iter(contexts)) if len(contexts) == 1 else "{}",
                    reason,
                ]
            )
    legacy = []
    for sid in config["legacy_selection_ids"]:
        result = store.inspect_selection(sid)
        row = _rows(
            store, "SELECT evidence,evidence_hash FROM frozen_selection WHERE selection_id=?", [sid]
        )[0]
        frozen = json.loads(row["evidence"])
        imports = frozen["inputs"]["imports"].values()
        if any(batch["parser_version"].startswith(("cito-", "lms-history-")) for batch in imports):
            raise ValueError(
                "Cito and retrospective bulk history are excluded from canonical inputs"
            )
        if not result:
            raise ValueError("Invalid legacy selection")
        legacy.append([dataset_id, sid, row["evidence_hash"]])
    supply_inputs = [_supply_input(store, item) for item in config["supply_imports"]]
    supply_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in supply_inputs:
        supply_groups[item["content_hash"]].append(item)
    supply = []
    for group in supply_groups.values():
        preferred = group
        if len(group) > 1:
            preference = config["supply_source_preference"]
            if any(item["source"] not in preference for item in group):
                raise ValueError("Duplicate supplier originals require explicit source preference")
            rank = min(preference.index(item["source"]) for item in group)
            preferred = [item for item in group if preference.index(item["source"]) == rank]
            if len(preferred) != 1:
                raise ValueError("Ambiguous supplier derivations; pin one verified interpretation")
        for item in group:
            selected = item in preferred
            supply.append(
                [
                    dataset_id,
                    item["import_id"],
                    item["verification_delivery_id"],
                    item["input_hash"],
                    item["rows_hash"],
                    item["row_count"],
                    selected,
                    None if selected else "duplicate_original_preferred_source",
                ]
            )
    supply.sort(key=lambda item: item[1])
    supplements = [_supplement_input(store, iid) for iid in config["supplement_import_ids"]]
    legacy_inputs = [_legacy_input(store, item) for item in config["legacy_imports"]]
    if len({item["delivery_id"] for item in legacy_inputs}) != len(legacy_inputs):
        raise ValueError("Pin exactly one reviewed legacy derivation per delivery")
    manifest = {
        "version": POLICY,
        "read_contract_hash": _read_contract_hash(),
        "native_price_value_policy": value_policy,
        "mode": "reconstructed_research",
        "training_authorized": False,
        "input_count": len(inputs),
        "period_count": len(periods),
        "member_states": dict(sorted(Counter(row[4] for row in choices).items())),
        "inputs_hash": digest({"inputs": inputs}),
        "evidence_deliveries": evidence,
        "choices_hash": digest(
            {"periods": periods, "members": choices, "legacy": legacy, "supply": supply}
        ),
        "supply_import_count": len(supply),
        "selected_supply_import_count": sum(row[6] for row in supply),
        "supply_identity": "source-scoped positive assertions; no implicit FTP identity join",
        "supplements": supplements,
        "supplement_identity": "source evidence only; no implicit package or DrugID episode join",
        "legacy_inputs": legacy_inputs,
        "legacy_identity": "delivery-scoped evidence only; no default model-price injection",
        "absence_means": "unknown; no deletion, recovery, zero price or forward fill",
        "identity": "causal package/DrugID/creation episodes; no permanent package merge",
    }
    with store.transaction():
        con = store._connection
        con.execute(
            "INSERT INTO analytical_dataset VALUES(?,?,?,?,?,?,?)",
            [
                dataset_id,
                POLICY,
                canonical_json(config),
                config_hash,
                canonical_json(manifest),
                digest(manifest),
                datetime.now(UTC),
            ],
        )
        con.executemany(
            "INSERT INTO registry_dataset_release VALUES(?,?,?,?,?)",
            [
                [
                    dataset_id,
                    iid,
                    value["auditor_version"],
                    value["evidence_hash"],
                    value["input_hash"],
                ]
                for iid, value in inputs.items()
            ],
        )
        con.executemany("INSERT INTO registry_dataset_period VALUES(?,?,?,?,?)", periods)
        con.executemany(
            "INSERT INTO registry_dataset_member VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", choices
        )
        if legacy:
            con.executemany("INSERT INTO registry_dataset_legacy VALUES(?,?,?)", legacy)
        if supply:
            con.executemany("INSERT INTO registry_dataset_supply VALUES(?,?,?,?,?,?,?,?)", supply)
        verified_result = status(store, dataset_id, verify=True, _native_reuse=_native_reuse)
        if _native_reuse is not None:
            _native_reuse.finish(store, config)
    return verified_result


def status(
    store: Store,
    dataset_id: str,
    *,
    verify: bool = False,
    _native_reuse: _NativeAttempt | None = None,
) -> dict[str, Any]:
    """Read compact coverage; optional verification checks all frozen bindings."""
    store._check_owner()
    if _native_reuse is not None:
        from .native_reuse import _NativeAttempt

        if not verify or not isinstance(_native_reuse, _NativeAttempt):
            raise ValueError("Opaque verifying native proof attempt required")
    rows = _rows(
        store, "SELECT * FROM analytical_dataset WHERE dataset_id=?", [str(UUID(dataset_id))]
    )
    if not rows:
        raise ValueError("Unknown analytical dataset")
    row = rows[0]
    config, manifest = json.loads(row["configuration"]), json.loads(row["manifest"])
    if digest(config) != row["configuration_hash"] or digest(manifest) != row["evidence_hash"]:
        raise ValueError("Frozen dataset metadata changed")
    if manifest.get("read_contract_hash") != _read_contract_hash():
        raise ValueError("Frozen dataset requires its original versioned query contract")
    if manifest.get("native_price_value_policy") != _value_policy_binding(config):
        raise ValueError("Frozen native price value policy binding changed")
    if verify:
        _value_policy_input(store, config)
        pinned = _rows(
            store,
            "SELECT * FROM registry_dataset_release WHERE dataset_id=? ORDER BY import_id",
            [dataset_id],
        )
        current = {}
        for item in pinned:
            iid = str(item["import_id"])
            current[iid] = _input(
                store, iid, item["auditor_version"], verify_original=_native_reuse is None
            )
            if current[iid]["input_hash"] != item["input_hash"]:
                raise ValueError("Frozen dataset source evidence changed")
        if digest({"inputs": current}) != manifest["inputs_hash"]:
            raise ValueError("Frozen dataset input inventory changed")
        if _native_reuse is None:
            _verify_typed(store, current)
        else:
            _native_reuse.consume(store, config, current)
        for eid, expected in manifest["evidence_deliveries"].items():
            if digest(_serial(store.get_record("source_delivery", eid))) != expected:
                raise ValueError("Semantic evidence changed")
            store.original_path(eid)
        periods = store._connection.execute(
            "SELECT * FROM registry_dataset_period WHERE dataset_id=? ORDER BY period_start,phase",
            [dataset_id],
        ).fetchall()
        members = store._connection.execute(
            "SELECT * FROM registry_dataset_member WHERE dataset_id=? "
            "ORDER BY period_start,phase,member_number",
            [dataset_id],
        ).fetchall()
        legacy = store._connection.execute(
            "SELECT * FROM registry_dataset_legacy WHERE dataset_id=? ORDER BY selection_id",
            [dataset_id],
        ).fetchall()
        supply = store._connection.execute(
            "SELECT * FROM registry_dataset_supply WHERE dataset_id=? ORDER BY import_id",
            [dataset_id],
        ).fetchall()
        if (
            digest(
                _serial(
                    {"periods": periods, "members": members, "legacy": legacy, "supply": supply}
                )
            )
            != manifest["choices_hash"]
        ):
            raise ValueError("Frozen dataset choices changed")
        for _, sid, expected in legacy:
            value = store._connection.execute(
                "SELECT evidence_hash FROM frozen_selection WHERE selection_id=?", [sid]
            ).fetchone()
            if value != (expected,):
                raise ValueError("Frozen legacy evidence changed")
            store.inspect_selection(str(sid))
        for _, iid, proof_id, input_hash, rows_hash, row_count, _, _ in supply:
            item = _supply_input(
                store, {"import_id": str(iid), "verification_delivery_id": str(proof_id)}
            )
            if (item["input_hash"], item["rows_hash"], item["row_count"]) != (
                input_hash,
                rows_hash,
                row_count,
            ):
                raise ValueError("Frozen supplier evidence changed")
        supplements = [
            _supplement_input(store, iid) for iid in config.get("supplement_import_ids", [])
        ]
        if supplements != manifest.get("supplements", []):
            raise ValueError("Frozen supplement evidence changed")
        legacy_inputs = [_legacy_input(store, item) for item in config.get("legacy_imports", [])]
        if legacy_inputs != manifest.get("legacy_inputs", []):
            raise ValueError("Frozen legacy source evidence changed")
    return {
        "dataset_id": dataset_id,
        "policy_version": POLICY,
        "configuration_hash": row["configuration_hash"],
        "evidence_hash": row["evidence_hash"],
        "manifest": manifest,
        "verified": verify,
    }


def _member_sql(number: int) -> str:
    if number not in LAYOUTS:
        raise ValueError("Unsupported registry member")
    keys = KEYS[number]
    fields = [field[0] for field in LAYOUTS[number][1]]
    expressions = []
    for field in fields:
        # Preserve the grouping key even when competing records disagree on values.
        unique = "" if field in keys else "count(DISTINCT r.row_id)=1 AND "
        expressions.append(
            f"CASE WHEN {unique}list_contains(any_value(s.supported_fields),'{field}') "
            f"THEN any_value(r.{field}) END AS {field}"
        )
    key_sql = ",".join(f"r.{key}" for key in keys)
    return f"""
      SELECT s.period_start,p.snapshot_date,s.phase,{number} AS member_number,
        json_array({key_sql})::VARCHAR AS entity_key,
        CASE WHEN count(DISTINCT r.row_id)=1 THEN 'present' ELSE 'conflict' END AS record_state,
        {",".join(expressions)},
        any_value(r.field_states)::VARCHAR AS native_field_states,
        any_value(s.supported_fields) AS supported_fields,
        any_value(s.import_id)::VARCHAR AS import_id,
        any_value(s.supporting_import_ids) AS supporting_import_ids,
        any_value(s.member_id) AS member_id,
        min(u.ordinal) AS source_record, count(*) AS source_occurrences,
        any_value(rb.native_name)||':record:'||min(u.ordinal)::VARCHAR AS source_locator,
        any_value(s.monetary_context)::VARCHAR AS monetary_context
      FROM registry_dataset_member s
      JOIN registry_dataset_period p USING(dataset_id,period_start,phase)
      JOIN registry_member m ON m.member_id=s.member_id
      JOIN registry_release_member rb ON rb.import_id=s.import_id
        AND rb.member_number=s.member_number,
      unnest(m.row_ids) WITH ORDINALITY u(row_id,ordinal)
      JOIN registry_lms{number:02} r USING(row_id)
      WHERE s.dataset_id=? AND s.member_number={number} AND s.state='selected'
      GROUP BY s.period_start,p.snapshot_date,s.phase,{key_sql}
    """


def _identity_sql() -> str:
    return f"""WITH native AS ({_member_sql(2)}),
      p AS (SELECT n.*,d.period_ordinal FROM native n JOIN registry_dataset_period d
        ON d.dataset_id=? AND d.period_start=n.period_start AND d.phase=n.phase),
      previous AS (SELECT *,lag(period_ordinal) OVER w AS last_ordinal,
        lag(drug_id) OVER w AS last_drug,lag(creation_date) OVER w AS last_creation,
        lag(exit_date) OVER w AS last_exit FROM p
        WINDOW w AS (PARTITION BY package_number,phase ORDER BY period_start)),
      breaks AS (SELECT *,CASE WHEN record_state!='present' OR drug_id IS NULL
        OR creation_date IS NULL OR creation_date=DATE '1900-01-01'
        OR creation_date>period_start OR last_ordinal IS NULL OR last_ordinal+1!=period_ordinal
        OR drug_id IS DISTINCT FROM last_drug OR creation_date IS DISTINCT FROM last_creation
        OR last_exit<=period_start THEN 1 ELSE 0 END AS episode_break FROM previous),
      numbered AS (SELECT *,sum(episode_break) OVER(PARTITION BY package_number,phase
        ORDER BY period_start ROWS UNBOUNDED PRECEDING) AS episode_number FROM breaks),
      episodes AS (SELECT *,min(period_start) OVER(PARTITION BY package_number,phase,episode_number)
        AS episode_start FROM numbered)
      SELECT *,CASE WHEN record_state='present' AND drug_id IS NOT NULL
        AND creation_date IS NOT NULL AND creation_date!=DATE '1900-01-01'
        AND creation_date<=period_start THEN
        sha256(json_array(package_number,drug_id,creation_date,phase,episode_start)::VARCHAR)
        END AS identity_episode_id FROM episodes"""


def _price_sql() -> str:
    cases = []
    for field, price_type in PRICE_FIELDS.items():
        cases.append(
            f"SELECT p.*, '{field}' AS field,'{price_type}' AS price_type,"
            f"p.{field} AS amount_ore FROM native_prices p"
        )
    no_fixed_codes = " OR ".join(
        "(d.code='" + code + "' AND d.description='" + description.replace("'", "''") + "')"
        for code, description in NO_FIXED_PRICE_CODES.items()
    )

    def supported(alias: str, fields: tuple[str, ...]) -> str:
        return f"{alias}.record_state='present' AND " + " AND ".join(
            f"list_contains({alias}.supported_fields,'{field}') AND "
            f"coalesce(json_extract_string({alias}.native_field_states,'$.{field}'),"
            f"'present')='present' AND {alias}.{field} IS NOT NULL"
            for field in fields
        )

    named_scope = (
        "p.phase='final' AND p.snapshot_date=p.period_start "
        f"AND p.period_start>=DATE '{NO_FIXED_PRICE_FROM}' "
        f"AND p.period_start<DATE '{NO_FIXED_PRICE_TO}' "
        "AND i.snapshot_date=p.snapshot_date"
    )
    no_fixed = (
        f"{named_scope} AND {supported('i', ('dispensing_code',))} "
        f"AND {supported('d', ('code', 'description'))} AND ({no_fixed_codes})"
    )
    not_calculated = (
        f"{named_scope} AND {supported('i', ('calculation_code',))} "
        f"AND {supported('c', ('code', 'description'))} "
        f"AND c.code='V' AND c.description='{NOT_CALCULATED_DESCRIPTION}'"
    )
    return f"""WITH ids AS ({_identity_sql()}),native_prices AS ({_member_sql(3)}),
      dispensing AS ({_member_sql(18)}),calculation AS ({_member_sql(13)}),
      values_by_field AS ({" UNION ALL ".join(cases)})
      SELECT p.period_start,p.snapshot_date,p.phase,p.package_number,i.drug_id,
        i.identity_episode_id,p.field,p.price_type,p.amount_ore,
        coalesce(json_extract_string(p.monetary_context,'$."3.'||p.field||'".currency'),
          'DKK') AS currency,
        coalesce(json_extract_string(p.monetary_context,'$."3.'||p.field||'".tax_basis'),
          'unknown') AS tax_basis,
        coalesce(json_extract_string(p.monetary_context,'$."3.'||p.field||'".fee_basis'),
          'unknown') AS fee_basis,
        coalesce(json_extract_string(p.monetary_context,'$."3.'||p.field||'".price_basis'),
          'per_package') AS price_basis,
        coalesce(json_extract_string(p.monetary_context,'$."3.'||p.field||'".price_role'),
          'effective_price') AS price_role,
        CASE WHEN NOT list_contains(p.supported_fields,p.field) THEN 'unsupported'
          WHEN p.record_state='conflict' THEN 'conflict'
          WHEN i.identity_episode_id IS NULL THEN 'unresolved_identity'
          WHEN p.amount_ore IS NULL THEN
            coalesce(json_extract_string(p.native_field_states,'$.'||p.field),'missing')
          WHEN p.field='consumer_ore' AND {no_fixed} THEN
            CASE WHEN p.amount_ore=0 THEN 'no_fixed_price' ELSE 'price_context_conflict' END
          WHEN p.field='registered_ore' AND {not_calculated} THEN
            CASE WHEN p.amount_ore=0 THEN 'not_calculated' ELSE 'price_context_conflict' END
          WHEN p.amount_ore=0 AND p.field IN
            ('aip_ore','registered_ore','consumer_ore','hospital_slot_08')
            THEN 'zero_value_unresolved'
          ELSE 'present' END AS state,
        p.import_id,p.supporting_import_ids,p.member_id,p.source_record,p.source_locator,
        'ftp' AS source_authority
      FROM values_by_field p LEFT JOIN ids i
        ON i.period_start=p.period_start AND i.phase=p.phase
          AND i.package_number=p.package_number
      LEFT JOIN dispensing d ON d.period_start=p.period_start AND d.phase=p.phase
        AND d.snapshot_date=p.snapshot_date AND d.code=i.dispensing_code
      LEFT JOIN calculation c ON c.period_start=p.period_start AND c.phase=p.phase
        AND c.snapshot_date=p.snapshot_date AND c.code=i.calculation_code"""


def _package_sql() -> str:
    """Join package/drug attributes only through the same frozen edition scope."""
    return f"""WITH packages AS ({_identity_sql()}),drugs AS ({_member_sql(1)}),
      companies AS ({_member_sql(9)})
      SELECT p.*,d.name,d.form,d.strength,d.atc,
        d.form_code,d.strength_numeric_token,d.strength_unit,
        d.authorisation_holder,d.drug_distributor,d.administration_codes,
        d.traffic_warning,d.substitution_flag,d.renewal_token,d.drug_substitution_group,
        d.dose_suitable,d.deregistration_date,d.quarantine_date,
        d.source_locator AS drug_source_locator,d.import_id AS drug_import_id,
        d.native_field_states AS drug_native_field_states,
        d.supported_fields AS drug_supported_fields,
        c.company_name AS package_distributor_name,c.source_locator AS company_source_locator,
        c.import_id AS company_import_id,c.record_state AS company_record_state,
        d.record_state AS drug_record_state
      FROM packages p LEFT JOIN drugs d ON d.period_start=p.period_start
        AND d.phase=p.phase AND d.drug_id=p.drug_id
      LEFT JOIN companies c ON c.period_start=p.period_start AND c.phase=p.phase
        AND c.company_code=p.package_distributor"""


def _legacy_price_sql() -> str:
    """Only explicit frozen F3 results can fill unsupported FTP field scopes.

    A compatible supported FTP scope blocks fallback even for absent packages or
    blank/conflicted values. Phase and all monetary dimensions remain distinct.
    Identity and already-frozen resolution decisions stay F3-scoped.
    """
    fields_case = (
        "CASE json_extract_string(v,'$.key.price_type') "
        + " ".join(
            f"WHEN '{price_type}' THEN '{field}'" for field, price_type in PRICE_FIELDS.items()
        )
        + " END"
    )
    context_match = " AND ".join(
        "coalesce(json_extract_string(s.monetary_context,'$.\"3.'||c.field||'\"."
        + name
        + f"'),'{default}')=json_extract_string(c.v,'$.key.{name}')"
        for name, default in (
            ("currency", "DKK"),
            ("tax_basis", "unknown"),
            ("fee_basis", "unknown"),
            ("price_basis", "per_package"),
            ("price_role", "effective_price"),
        )
    )
    return f"""WITH cells AS (
      SELECT l.dataset_id,e.value AS v FROM registry_dataset_legacy l
      JOIN frozen_selection f USING(selection_id),json_each(f.evidence,'$.result') e
      WHERE l.dataset_id=? AND json_extract_string(e.value,'$.key.table')='price_observation'
        AND json_extract_string(e.value,'$.key.field')='amount_ore'
        AND json_extract_string(e.value,'$.key.price_role')='effective_price'),
      values_by_key AS (SELECT c.*,m.canonical_value AS package_number,
        json_extract_string(v,'$.key.period')::DATE AS period_start,
        coalesce(json_extract_string(v,'$.key.phase'),(
          SELECT CASE WHEN count(DISTINCT coalesce(
            json_extract_string(a.value,'$.phase'),'unknown'))=1
            THEN min(coalesce(json_extract_string(a.value,'$.phase'),'unknown'))
            ELSE 'unknown' END
          FROM json_each(v,'$.assertions') a WHERE json_contains(
            json_extract(v,'$.supporting_observation_ids'),
            json_extract(a.value,'$.observation_id'))),'unknown') AS phase,
        {fields_case} AS field FROM cells c LEFT JOIN package_identifier m
        ON m.mapping_id=try_cast(json_extract_string(v,'$.mapping_ids[0]') AS UUID)),
      permitted AS (SELECT * FROM values_by_key c WHERE c.period_start IS NOT NULL
        AND c.package_number IS NOT NULL AND c.field IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM registry_dataset_member s WHERE s.dataset_id=c.dataset_id
            AND s.period_start=c.period_start AND s.member_number=3
            AND s.phase=c.phase AND list_contains(s.supported_fields,c.field)
            AND {context_match})),
      grouped AS (SELECT period_start,phase,package_number,field,
        json_extract_string(v,'$.key.price_type') AS price_type,
        json_extract_string(v,'$.key.currency') AS currency,
        json_extract_string(v,'$.key.tax_basis') AS tax_basis,
        json_extract_string(v,'$.key.fee_basis') AS fee_basis,
        json_extract_string(v,'$.key.price_basis') AS price_basis,
        json_extract_string(v,'$.key.price_role') AS price_role,
        count(DISTINCT json_object('state',json_extract_string(v,'$.state'),
          'value',json_extract(v,'$.value'),
          'identity',json_extract_string(v,'$.key.package_id'))) AS versions,
        any_value(try_cast(json_extract_string(v,'$.value') AS BIGINT)) AS value,
        bool_and(coalesce(try_cast(json_extract_string(v,'$.usable') AS BOOLEAN),false)) AS usable,
        any_value(json_extract_string(v,'$.state')) AS original_state,
        any_value(json_extract_string(v,'$.key.package_id')) AS package_id
        FROM permitted GROUP BY ALL)
      SELECT period_start,NULL::DATE AS snapshot_date,phase,
        package_number,NULL::VARCHAR AS drug_id,'legacy:'||package_id AS identity_episode_id,
        field,price_type,CASE WHEN versions=1 THEN value END AS amount_ore,
        currency,tax_basis,fee_basis,
        price_basis,price_role,CASE WHEN versions>1 THEN 'conflict'
          WHEN NOT usable THEN 'excluded'
          ELSE original_state END AS state,NULL::VARCHAR AS import_id,
        []::UUID[] AS supporting_import_ids,NULL::BIGINT AS member_id,
        NULL::BIGINT AS source_record,NULL::VARCHAR AS source_locator,
        'verified_legacy' AS source_authority
      FROM grouped"""


def _supply_sql() -> str:
    return """SELECT o.observation_date,o.canonical_identifier AS package_number,
      o.wholesaler_namespace,o.wholesaler_identifier,
      CASE WHEN count(DISTINCT o.reported_status)=1
        THEN any_value(o.reported_status) END AS reported_status,
      CASE WHEN count(DISTINCT o.reported_status)>1 THEN 'conflict'
        WHEN bool_and(o.quality_state='valid') THEN 'present' ELSE 'excluded' END AS state,
      'source_only_no_longitudinal_identity' AS identity_state,
      NULL::VARCHAR AS identity_episode_id,
      list(DISTINCT o.observation_id ORDER BY o.observation_id) AS supporting_observation_ids,
      list(DISTINCT o.import_id ORDER BY o.import_id) AS supporting_import_ids,
      'unknown' AS absence_means
      FROM registry_dataset_supply s JOIN supply_observation o USING(import_id)
      WHERE s.dataset_id=? AND s.selected
      GROUP BY o.observation_date,o.canonical_identifier,
        o.wholesaler_namespace,o.wholesaler_identifier"""


def _supplement_editions_sql() -> str:
    return """SELECT s.kind,d.source,d.content_hash AS edition_hash,r.import_id,
      s.delivery_id,r.source_line,r.byte_offset,r.byte_length,r.assertion,
      r.package_code AS package_number,r.drug_id,r.extended_prescribing_code,
      r.issue_date,r.effective_date,r.valid_from,r.valid_to,
      json_extract_string(r.payload,'$.source_description') AS source_description,
      json_extract_string(r.payload,'$.atc_code') AS atc_code,
      json_extract_string(r.payload,'$.professional_group') AS professional_group,
      json_extract_string(r.payload,'$.specialty_name') AS specialty_name,
      s.kind||':line:'||r.source_line::VARCHAR||':byte:'||r.byte_offset::VARCHAR AS source_locator,
      r.payload::VARCHAR AS native_payload,s.metadata::VARCHAR AS source_metadata,
      'evidence_only' AS grain,'unknown' AS absence_means,
      NULL::VARCHAR AS identity_episode_id,false AS training_usable
      FROM analytical_dataset a,json_each(a.configuration,'$.supplement_import_ids') p
      JOIN supplement_import s ON s.import_id=json_extract_string(p.value,'$')::UUID
      JOIN supplement_record r ON r.import_id=s.import_id
      JOIN source_delivery d ON d.delivery_id=s.delivery_id WHERE a.dataset_id=?"""


def _supplement_sql() -> str:
    """Deduplicate identical scoped assertions, never infer an undated validity."""
    return f"""WITH native AS ({_supplement_editions_sql()}),scoped AS (
      SELECT *,json_array(kind,source,assertion,package_number,drug_id,
        extended_prescribing_code,issue_date,effective_date,valid_from,valid_to)::VARCHAR
        AS business_key,
        CASE WHEN kind='LMS37' OR (kind='LMS36' AND
          (valid_from IS NULL OR valid_from=DATE '1970-01-01'))
          THEN edition_hash END AS edition_scope,
        json_array(source_description,atc_code,professional_group,specialty_name)::VARCHAR
          AS semantic_value
      FROM native),variants AS (
      SELECT *,count(DISTINCT semantic_value) OVER(PARTITION BY business_key)
        AS cross_edition_value_variants FROM scoped)
      SELECT kind,source,assertion,package_number,drug_id,extended_prescribing_code,
        issue_date,effective_date,valid_from,valid_to,edition_scope,
        sha256(json_array(business_key,edition_scope)::VARCHAR) AS assertion_key,
        CASE WHEN count(DISTINCT semantic_value)>1 THEN 'conflict' ELSE 'present' END AS state,
        CASE WHEN count(DISTINCT semantic_value)=1 THEN any_value(source_description)
          END AS source_description,
        CASE WHEN count(DISTINCT semantic_value)=1 THEN any_value(atc_code) END AS atc_code,
        CASE WHEN count(DISTINCT semantic_value)=1 THEN any_value(professional_group)
          END AS professional_group,
        CASE WHEN count(DISTINCT semantic_value)=1 THEN any_value(specialty_name)
          END AS specialty_name,
        max(cross_edition_value_variants) AS cross_edition_value_variants,
        max(cross_edition_value_variants)>1 AS cross_edition_disagreement,
        list(DISTINCT import_id ORDER BY import_id) AS supporting_import_ids,
        list(DISTINCT json_object('import_id',import_id,'source_locator',source_locator)
          ORDER BY json_object('import_id',import_id,'source_locator',source_locator))
          AS supporting_source_locators,
        count(*) AS source_occurrences,
        CASE WHEN kind='DOK' THEN 'native_effective_date'
          WHEN kind='LMS37' THEN 'undated_reference_edition'
          WHEN valid_from=DATE '1970-01-01' THEN 'sentinel_validity_unresolved'
          ELSE 'native_validity_assertion' END AS temporal_state,
        'source_only_no_longitudinal_identity' AS identity_state,
        NULL::VARCHAR AS identity_episode_id,'evidence_only' AS grain,
        false AS training_usable,'unknown' AS absence_means
      FROM variants GROUP BY kind,source,assertion,package_number,drug_id,
        extended_prescribing_code,issue_date,effective_date,valid_from,valid_to,
        edition_scope,business_key"""


def _legacy_editions_sql() -> str:
    from .ingestion import TABLE_FIELDS

    parts = []
    for table, fields in TABLE_FIELDS.items():
        if table == "supply_observation":
            continue
        contexts = {
            "price_observation": (
                "price_type",
                "price_role",
                "currency",
                "tax_basis",
                "fee_basis",
                "price_basis",
            ),
            "classification_observation": (
                "classification_context",
                "group_namespace",
                "group_scope",
            ),
            "lifecycle_event": ("event_type",),
        }.get(table, ())
        context_sql = "json_object(" + ",".join(f"'{field}',o.{field}" for field in contexts) + ")"
        value_fields = (*fields, "quality_state", "field_presence")
        value_sql = (
            "json_object(" + ",".join(f"'{field}',o.{field}" for field in value_fields) + ")"
        )
        parts.append(f"""SELECT '{table}' AS observation_table,d.source,
          d.content_hash AS edition_hash,
          i.parser_version,i.interpretation_context_hash,d.phase,o.observation_id,o.import_id,
          o.delivery_id,o.source_locator,o.identifier_namespace,o.source_identifier,
          o.canonical_identifier AS package_number,o.package_id AS source_package_id,o.mapping_id,
          o.effective_period,o.report_period,o.effective_date,o.quality_state,
          o.field_presence::VARCHAR AS field_presence,o.source_values::VARCHAR AS source_values,
          {",".join("o." + field for field in fields)},
          {context_sql}::VARCHAR AS assertion_context,{value_sql}::VARCHAR AS semantic_value,
          'source_only_no_longitudinal_identity' AS identity_state,
          NULL::VARCHAR AS identity_episode_id,'evidence_only' AS grain,
          false AS training_usable,'unknown' AS absence_means
          FROM analytical_dataset a,json_each(a.configuration,'$.legacy_imports') p
          JOIN {table} o ON o.import_id=json_extract_string(p.value,'$.import_id')::UUID
          JOIN import_batch i ON i.import_id=o.import_id
          JOIN source_delivery d ON d.delivery_id=o.delivery_id WHERE a.dataset_id=?""")
    return " UNION ALL BY NAME ".join(parts)


def _legacy_evidence_sql() -> str:
    from .ingestion import TABLE_FIELDS

    fields = sorted(
        {
            field
            for table, fields in TABLE_FIELDS.items()
            if table != "supply_observation"
            for field in fields
        }
    )
    values = ",".join(
        f"CASE WHEN count(DISTINCT semantic_value)=1 THEN any_value({field}) END AS {field}"
        for field in [*fields, "quality_state", "field_presence"]
    )
    return f"""WITH native AS ({_legacy_editions_sql()})
      SELECT observation_table,source,edition_hash,parser_version,interpretation_context_hash,
        phase,package_number,effective_period,report_period,effective_date,assertion_context,
        {values},CASE WHEN count(DISTINCT semantic_value)>1 THEN 'conflict'
          WHEN any_value(quality_state)='valid' THEN 'present'
          ELSE any_value(quality_state) END AS state,
        list(DISTINCT observation_id ORDER BY observation_id) AS supporting_observation_ids,
        list(DISTINCT import_id ORDER BY import_id) AS supporting_import_ids,
        list(DISTINCT json_object('import_id',import_id,'source_locator',source_locator)
          ORDER BY json_object('import_id',import_id,'source_locator',source_locator))
          AS supporting_source_locators,
        count(*) AS source_occurrences,'source_only_no_longitudinal_identity' AS identity_state,
        NULL::VARCHAR AS identity_episode_id,'evidence_only' AS grain,false AS training_usable,
        'unknown' AS absence_means
      FROM native GROUP BY observation_table,source,edition_hash,parser_version,
        interpretation_context_hash,phase,package_number,effective_period,report_period,
        effective_date,assertion_context"""


def _query_sql(domain: str | int) -> tuple[str, int, tuple[str, ...]]:
    if isinstance(domain, int):
        return _member_sql(domain), 1, ("period_start", "phase", "entity_key")
    if domain in {"packages", "package_observation"}:
        return _package_sql(), 4, ("period_start", "phase", "package_number")
    if domain in {"prices", "price_observation"}:
        combined = (
            f"SELECT * FROM ({_price_sql()}) ftp UNION ALL "
            f"SELECT * FROM ({_legacy_price_sql()}) legacy"
        )
        return (
            f"SELECT *,state='present' AND tax_basis!='unknown' AND fee_basis!='unknown' AS usable,"
            f"coalesce(amount_ore>0,false) AS positive_reference FROM ({combined}) resolved",
            6,
            (
                "period_start",
                "phase",
                "package_number",
                "price_type",
                "currency",
                "tax_basis",
                "fee_basis",
                "price_basis",
                "price_role",
            ),
        )
    if domain in {"supply", "supply_observation"}:
        return (
            _supply_sql(),
            1,
            ("observation_date", "package_number", "wholesaler_namespace", "wholesaler_identifier"),
        )
    if domain == "supplements":
        return _supplement_sql(), 1, ("kind", "assertion_key")
    if domain == "supplement_editions":
        return _supplement_editions_sql(), 1, ("kind", "import_id", "source_line")
    if domain == "legacy_evidence":
        return (
            _legacy_evidence_sql(),
            4,
            (
                "observation_table",
                "source",
                "edition_hash",
                "parser_version",
                "interpretation_context_hash",
                "phase",
                "package_number",
                "effective_period",
                "report_period",
                "effective_date",
                "assertion_context",
            ),
        )
    if domain == "legacy_editions":
        return _legacy_editions_sql(), 4, ("observation_table", "import_id", "observation_id")
    if domain in {"classifications", "classification_observation"}:
        return _member_sql(4), 1, ("period_start", "phase", "entity_key")
    if domain == "coverage":
        return (
            "SELECT period_start,phase,member_number,state,reason,complete_snapshot,"
            "supported_fields,candidate_import_ids FROM registry_dataset_member WHERE dataset_id=?",
            1,
            ("period_start", "phase", "member_number"),
        )
    if domain == "editions":
        return (
            "SELECT r.import_id,r.effective_date,r.tariff_week,r.phase,r.profile,"
            "r.delivery_id,d.content_hash,d.published_precision,d.received_precision,"
            "p.auditor_version,p.evidence_hash FROM registry_dataset_release p "
            "JOIN registry_release r USING(import_id) "
            "JOIN source_delivery d ON d.delivery_id=r.delivery_id WHERE p.dataset_id=?",
            1,
            ("effective_date", "phase", "import_id"),
        )
    raise ValueError(
        "Choose prices, packages, classifications, supply, supplements, supplement_editions, "
        "legacy_evidence, legacy_editions, "
        "coverage, editions or an integer native reference member"
    )


def _read_contract_hash() -> str:
    domains: list[str | int] = [
        "prices",
        "packages",
        "classifications",
        "supply",
        "supplements",
        "supplement_editions",
        "legacy_evidence",
        "legacy_editions",
        "coverage",
        "editions",
        *sorted(LAYOUTS),
    ]
    return digest(
        {
            "policy": POLICY,
            "native_price_value_contract": native_price_value_contract(),
            "queries": {
                str(domain): {"sql": sql, "parameter_count": count, "ordering": list(order)}
                for domain in domains
                for sql, count, order in [_query_sql(domain)]
            },
        }
    )


def query(
    store: Store,
    dataset_id: str,
    domain: str | int,
    *,
    period_start: str | None = None,
    package_number: str | None = None,
    limit: int = 1000,
    offset: int = 0,
    verify: bool = True,
) -> list[dict[str, Any]]:
    """Bounded deterministic read; integers, dates and missing states remain typed.

    Integer domains expose all supported native fields at their own natural grain.
    Coverage includes absent/conflicting members even when they produce no records.
    """
    store._check_owner()
    status(store, dataset_id, verify=verify)
    if type(limit) is not int or not 1 <= limit <= 100_000 or type(offset) is not int or offset < 0:
        raise ValueError("Bounded query limit/offset required")
    sql, count, order = _query_sql(domain)
    parameters: list[Any] = [dataset_id] * count
    filters = []
    if period_start is not None:
        if domain in {
            "supply",
            "supply_observation",
            "supplements",
            "supplement_editions",
            "editions",
            "legacy_evidence",
            "legacy_editions",
        }:
            raise ValueError("This domain has native dates, not a tariff-period grain")
        filters.append("period_start=?")
        parameters.append(_date(period_start))
    if package_number is not None:
        if len(package_number) != 6 or not package_number.isascii() or not package_number.isdigit():
            raise ValueError("Canonical six-digit package identifier required")
        filters.append("package_number=?")
        parameters.append(package_number)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    return _rows(
        store,
        f"SELECT * FROM ({sql}) q{where} ORDER BY {','.join(order)} LIMIT ? OFFSET ?",
        [*parameters, limit, offset],
    )


def extract(store: Store, dataset_id: str, domain: str | int, destination: Path) -> dict[str, Any]:
    """Stream a deterministic CSV without materializing the full historical panel."""
    from .archive import external_path

    store._check_owner()
    proof = status(store, dataset_id, verify=True)
    destination = external_path(destination)
    if destination.exists():
        raise ValueError("Extraction requires a new destination")
    sql, count, order = _query_sql(domain)
    result = store._connection.execute(
        f"SELECT * FROM ({sql}) q ORDER BY {','.join(order)}", [dataset_id] * count
    )
    names = [column[0] for column in result.description]
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with destination.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(names)
        while batch := result.fetchmany(4096):
            for row in batch:
                writer.writerow(
                    [
                        json.dumps(_serial(value), ensure_ascii=False)
                        if isinstance(value, (list, dict))
                        else value
                        for value in row
                    ]
                )
            total += len(batch)
    sha = hashlib.sha256()
    with destination.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            sha.update(block)
    return {
        "dataset_id": dataset_id,
        "evidence_hash": proof["evidence_hash"],
        "domain": domain,
        "rows": total,
        "sha256": sha.hexdigest(),
        "format": "csv; empty nullable cells retain explicit state columns",
    }
