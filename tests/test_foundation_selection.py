"""Independently specified F3 examples; no workbook adapter derives expectations."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import duckdb
import pytest

from bef_redbird.foundation import FoundationError, Store, Timing
from bef_redbird.foundation.ingestion import TABLE_FIELDS
from bef_redbird.foundation.recovery import backup_store, restore_store
from bef_redbird.foundation.selection import POLICY, CatalogueSelector, _configuration, serial

PERIOD = "2026-01-05"
NEXT = "2026-01-19"
WEDNESDAY = "2026-01-14T12:00:00+00:00"
MONDAY = "2026-01-12T09:00:00+00:00"
FRIDAY = "2026-01-16T18:00:00+00:00"


def uid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, "f3-synthetic-tests/" + label))


def timing(instant: str | None = MONDAY) -> Timing:
    if instant is None:
        return Timing()
    value = datetime.fromisoformat(instant)
    return Timing(value, value, "instant", "invented source evidence", "UTC")


def independent_hash(record: dict[str, Any]) -> str:
    # Deliberately uses stdlib encoding, not production record_hash/digest.
    data = json.dumps(
        record,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(data.encode()).hexdigest()


class Fixture:
    def __init__(self, store: Store, root: Path) -> None:
        self.store, self.root = store, root
        self.rows: list[dict[str, Any]] = []
        self.imports: list[str] = []
        self.mappings: list[str] = []
        self.expected_issues: dict[str, int] = {}
        self.report_sequence = 0
        self.packages = [uid("package-seven"), uid("package-eight")]
        for period in (PERIOD, NEXT):
            start = date.fromisoformat(period)
            store.add_period(start, start + timedelta(days=14))
        for package in self.packages:
            store.create_package(package)
        source = self.document("identity", {"7": self.packages[0], "8": self.packages[1]})
        batch = store.begin_import(
            source,
            parser_version="independent-evidence-v1",
            schema_version="explicit-identity-v1",
            mapping_version="synthetic-v1",
        )
        store.finish_import(
            batch, status="accepted", record_count=2, accepted_count=2, rejected_count=0
        )
        for index, package in enumerate(self.packages):
            self.mappings.append(
                store.add_identifier(
                    package,
                    namespace="dk:varenummer",
                    identifier_value=str(index + 7),
                    evidence_import_id=batch,
                    source_locator=f"key:{index + 7}",
                    mapping_version="synthetic-v1",
                    validity_kind="interval",
                    valid_from=date(2026, 1, 5),
                    valid_to=date(2026, 2, 2),
                    available=timing(),
                )
            )

    def document(self, label: str, value: Any, **kwargs: Any) -> str:
        path = self.root / f"{label}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return self.store.register_source(
            path,
            source=kwargs.pop("source", label),
            receipt_key=label,
            received=kwargs.pop("received", timing()),
            published=kwargs.pop("published", timing()),
            **kwargs,
        )

    def source(
        self,
        label: str,
        rows: list[dict[str, Any]],
        *,
        kind: str = "partial_extract",
        received: Timing | None = None,
        published: Timing | None = None,
        delivery: str | None = None,
        version: str = "explicit-v1",
        rejected: int = 0,
        source: str | None = None,
        issues: list[dict[str, Any]] | None = None,
    ) -> str:
        delivery = delivery or self.document(
            label,
            rows,
            source=source or label,
            received=received if received is not None else timing(),
            published=published if published is not None else timing(),
            current_period=date.fromisoformat(PERIOD),
            coverage={"version": 1, "kind": kind},
        )
        batch = self.store.begin_import(
            delivery,
            parser_version=version,
            schema_version="typed-observations-v1",
            mapping_version="synthetic-v1",
        )
        for index, supplied in enumerate(rows):
            values = dict(supplied)
            table = values.pop("table", "price_observation")
            package_index = values.pop("package_index", 0)
            presence = values.pop("presence", {})
            common = {
                "effective_period": PERIOD,
                "report_period": PERIOD,
                "effective_date": None,
                "quality_state": "valid",
            }
            common.update({key: values.pop(key) for key in list(values) if key in common})
            business = dict.fromkeys(TABLE_FIELDS[table])
            if table == "price_observation":
                business.update(
                    price_type="aip",
                    amount_ore=100,
                    currency="DKK",
                    price_basis="per_package",
                    tax_basis="excluded",
                    fee_basis="excluded",
                    price_role="effective_price",
                )
            business.update(values)
            fields = {
                key: presence.get(key, "present" if val is not None else "unreported")
                for key, val in business.items()
            }
            record = {
                "observation_id": uid(f"{batch}/{table}/{index}"),
                "import_id": batch,
                "delivery_id": delivery,
                "package_id": self.packages[package_index],
                "mapping_id": self.mappings[package_index],
                "source_locator": f"record:{index + 1}",
                "identifier_namespace": "dk:varenummer",
                "source_identifier": str(package_index + 7),
                "canonical_identifier": str(package_index + 7).zfill(6),
                **common,
                "field_presence": json.dumps(fields),
                "source_values": json.dumps(supplied),
                **business,
            }
            self.store._insert(table, record)
            self.rows.append(record)
        for problem in issues or []:
            self.store.add_issue(batch, **problem)
        self.expected_issues[batch] = len(issues or [])
        self.store.finish_import(
            batch,
            status="accepted",
            record_count=len(rows) + rejected,
            accepted_count=len(rows),
            rejected_count=rejected,
        )
        self.imports.append(batch)
        return batch

    def config(self, **changes: Any) -> dict[str, Any]:
        # Fetch exact persisted representations solely for the verification binding.
        # Expected business values in every test are independent constants.
        records = []
        for table in TABLE_FIELDS:
            result = self.store._connection.execute(f"SELECT * FROM {table}")
            columns = [col[0] for col in result.description]
            records.extend(dict(zip(columns, row, strict=True)) for row in result.fetchall())
        self.report_sequence += 1
        report = self.document(
            f"verification-{self.report_sequence}",
            {
                "version": 1,
                "complete": True,
                "mismatches": [],
                "quarantined_observation_ids": [],
                "mapping_hashes": {
                    str(row[0]): independent_hash(
                        self.store.get_record("package_identifier", str(row[0]))
                    )
                    for row in self.store._connection.execute(
                        "SELECT mapping_id FROM package_identifier"
                    ).fetchall()
                },
                "verified_observation_ids": [str(row["observation_id"]) for row in records],
                "observation_hashes": {
                    str(row["observation_id"]): independent_hash(row) for row in records
                },
                "imports": [
                    {
                        "import_id": batch,
                        "status": "verified",
                        "mismatch_count": 0,
                        "import_record_hash": independent_hash(
                            self.store.get_record("import_batch", batch)
                        ),
                        "expected_assertions": sum(row["import_id"] == batch for row in self.rows),
                        "actual_assertions": sum(str(row["import_id"]) == batch for row in records),
                        "expected_issues": self.expected_issues.get(batch, 0),
                        "actual_issues": len(self.store.issues(batch)),
                        "source_rows": sum(row["import_id"] == batch for row in self.rows),
                        "independent_schema": {
                            "kind": "explicit independently authored JSON records"
                        },
                    }
                    for batch in self.imports
                ],
            },
        )
        config = {
            "version": 1,
            "policy_version": POLICY,
            "import_ids": self.imports.copy(),
            "mapping_ids": self.mappings.copy(),
            "derivations": {
                str(self.store.get_record("import_batch", batch)["delivery_id"]): batch
                for batch in self.imports
            },
            "scope": {
                "package_ids": self.packages,
                "periods": [PERIOD, NEXT],
                "dates": [PERIOD],
                "tables": list(TABLE_FIELDS),
            },
            "cutoff": WEDNESDAY,
            "mode": "historical_replay",
            "availability_policy": "receipt",
            "source_preference": {"version": "none-v1", "rules": []},
            "verification_reports": [report],
            "context_evidence": {batch: [] for batch in self.imports},
            "revisions": [],
            "business_context": {"purpose": "independently expected synthetic example"},
        }
        config.update(changes)
        return config

    def operation(
        self,
        label: str,
        new: str,
        old: list[str],
        *,
        kind: str,
        table: str = "price_observation",
        fields: list[str] | None = None,
        **changes: Any,
    ) -> str:
        value = {
            "version": 1,
            "kind": kind,
            "replacement_import": new,
            "supersedes": old,
            "scope": {
                "package_ids": self.packages,
                "periods": [PERIOD],
                "dates": [],
                "tables": [table],
                "fields": fields or ["amount_ore"],
            },
            "expected_observation_ids": [
                row["observation_id"] for row in self.rows if row["import_id"] == new
            ],
            "expected_members": ["main"],
            "members": {"main": new},
            "source_locator": "explicit independently authored revision contract",
        }
        value.update(changes)
        return self.document(label, value)


@pytest.fixture
def fixture(tmp_path: Path):
    with Store(tmp_path / "selection.duckdb", tmp_path / "originals") as store:
        yield Fixture(store, tmp_path)


def results(fixture: Fixture, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return fixture.store.create_selection(config or fixture.config())["evidence"]["result"]


@pytest.mark.parametrize("complete", [True, False])
def test_compressed_verification_retains_exact_admission_gates(
    fixture: Fixture, complete: bool
) -> None:
    fixture.source("source", [{"amount_ore": 1255}])
    config = fixture.config()
    report = json.loads(fixture.store.original_path(config["verification_reports"][0]).read_bytes())
    report["complete"] = complete
    path = fixture.root / "verification.json.gz"
    path.write_bytes(gzip.compress(json.dumps(report).encode(), mtime=0))
    delivery = fixture.store.register_source(
        path, source="compressed-proof", receipt_key="compressed-proof", received=timing()
    )
    config["verification_reports"] = [delivery]
    if complete:
        selected = results(fixture, config)
        assert len(selected) == 1
        assert selected[0]["value"] == 1255
        assert selected[0]["usable"] is True
    else:
        with pytest.raises(ValueError, match="incomplete or failed"):
            results(fixture, config)


def test_equivalent_assertions_corroborate_without_multiplying_values(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 1255}])
    fixture.source("two", [{"amount_ore": 1255}])
    selected = results(fixture)
    assert len(selected) == 1
    assert selected[0]["value"] == 1255
    assert selected[0]["usable"] is True
    assert len(selected[0]["supporting_observation_ids"]) == 2


def test_alternative_derivation_is_explicit_and_counted_once(fixture: Fixture) -> None:
    first = fixture.source("one", [{"amount_ore": 1255}])
    delivery = str(fixture.store.get_record("import_batch", first)["delivery_id"])
    second = fixture.source("two", [{"amount_ore": 7777}], delivery=delivery, version="explicit-v2")
    config = fixture.config(derivations={delivery: first})
    selection = fixture.store.create_selection(config)
    assert selection["evidence"]["result"][0]["value"] == 1255
    assert {entry["reason"] for entry in selection["evidence"]["exclusions"]} == {
        "alternative_derivation"
    }
    assert second in selection["evidence"]["inputs"]["imports"]
    with pytest.raises(ValueError, match="derivation"):
        fixture.store.create_selection({**config, "derivations": {}})


@pytest.mark.parametrize("other", [101, 0])
def test_material_disagreement_excludes_calculation(fixture: Fixture, other: int) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    fixture.source("two", [{"amount_ore": other}])
    selected = results(fixture)[0]
    assert selected["state"] == "conflict"
    assert selected["usable"] is False
    assert selected["value"] is None
    assert len(selected["assertions"]) == 2


def test_accepted_invalid_assertion_blocks_affected_value(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    fixture.source(
        "two",
        [
            {
                "amount_ore": None,
                "presence": {"amount_ore": "invalid"},
                "quality_state": "invalid",
            }
        ],
    )
    selected = results(fixture)[0]
    assert selected["state"] == "excluded"
    assert selected["usable"] is False


@pytest.mark.parametrize(
    "clock",
    [
        timing(FRIDAY),
        timing(None),
        Timing(
            datetime.fromisoformat(MONDAY),
            datetime.fromisoformat(FRIDAY),
            "range",
            "independent uncertainty",
            "UTC",
        ),
    ],
)
def test_friday_unknown_and_overlapping_bounds_are_excluded(
    fixture: Fixture,
    clock: Timing,
) -> None:
    fixture.source("one", [{"amount_ore": 100}], received=clock)
    selection = fixture.store.create_selection(fixture.config())
    assert not selection["evidence"]["result"]
    assert any(
        item["reason"] == "delivery_unavailable" for item in selection["evidence"]["exclusions"]
    )


def test_upper_bound_at_cutoff_and_publication_policy(fixture: Fixture) -> None:
    fixture.source(
        "one", [{"amount_ore": 100}], received=timing(FRIDAY), published=timing(WEDNESDAY)
    )
    selection = fixture.store.create_selection(fixture.config(availability_policy="publication"))
    assert selection["evidence"]["result"][0]["value"] == 100
    assert "does not establish pharmacy possession" in selection["evidence"]["availability_claim"]


def test_research_discloses_unknown_and_late_evidence(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}], received=timing(None))
    selection = fixture.store.create_selection(fixture.config(mode="reconstructed_research"))
    assert selection["evidence"]["result"][0]["value"] == 100
    assert selection["evidence"]["availability_disclosures"]
    assert "no demonstrated historical availability" in selection["evidence"]["availability_claim"]


def test_live_has_actual_production_time_and_lateness(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    selection = fixture.store.create_selection(fixture.config(mode="live"))
    assert selection["evidence"]["late"] is True
    assert datetime.fromisoformat(selection["produced_at"]) > datetime.fromisoformat(WEDNESDAY)
    with pytest.raises(ValueError, match="Live"):
        fixture.store.create_selection(
            fixture.config(mode="live", availability_policy="publication")
        )


def test_later_mapping_and_mapping_evidence_are_both_checked(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    original = fixture.store.get_record("package_identifier", fixture.mappings[0])
    later = fixture.store.add_identifier(
        fixture.packages[0],
        namespace="dk:varenummer",
        identifier_value="7",
        evidence_import_id=str(original["evidence_import_id"]),
        source_locator="late correction",
        mapping_version="later-v2",
        validity_kind="interval",
        valid_from=date(2026, 1, 5),
        valid_to=date(2026, 2, 2),
        available=timing(FRIDAY),
    )
    assert results(fixture, fixture.config(mapping_ids=[later])) == []
    evidence_delivery = fixture.document(
        "late-mapping-evidence", {"meaning": "synthetic"}, received=timing(FRIDAY)
    )
    evidence_import = fixture.store.begin_import(
        evidence_delivery,
        parser_version="mapping-v1",
        schema_version="mapping-v1",
        mapping_version="v3",
    )
    fixture.store.finish_import(
        evidence_import, status="accepted", record_count=1, accepted_count=1, rejected_count=0
    )
    early_claim = fixture.store.add_identifier(
        fixture.packages[0],
        namespace="dk:varenummer",
        identifier_value="7",
        evidence_import_id=evidence_import,
        source_locator="late original",
        mapping_version="v3",
        validity_kind="interval",
        valid_from=date(2026, 1, 5),
        valid_to=date(2026, 2, 2),
        available=timing(),
    )
    assert results(fixture, fixture.config(mapping_ids=[early_claim])) == []


def test_delivery_identity_cannot_join_another_receipt(fixture: Fixture) -> None:
    first = fixture.source("one", [{"amount_ore": 100}])
    fixture.source("two", [{"amount_ore": 100}])
    delivery = str(fixture.store.get_record("import_batch", first)["delivery_id"])
    scoped = fixture.store.add_identifier(
        fixture.packages[0],
        namespace="dk:varenummer",
        identifier_value="7",
        evidence_import_id=first,
        source_locator="record:1",
        mapping_version="delivery-only",
        validity_kind="scope",
        scope_key=f"delivery:{delivery}",
        available=timing(),
    )
    selected = results(fixture, fixture.config(mapping_ids=[scoped]))
    assert len(selected) == 1
    assert len(selected[0]["supporting_observation_ids"]) == 1


@pytest.mark.parametrize(
    "difference",
    [
        {"price_type": "registered"},
        {"price_type": "consumer"},
        {"currency": "EUR"},
        {"price_basis": "per_unit"},
        {"tax_basis": "included"},
        {"fee_basis": "included"},
        {"price_role": "reported_previous_price", "effective_period": None},
    ],
)
def test_incompatible_price_contexts_remain_separate(
    fixture: Fixture,
    difference: dict[str, Any],
) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    fixture.source("two", [{"amount_ore": 900, **difference}])
    # Unknown effective previous-price interval cannot borrow a report date for identity.
    if difference.get("price_role"):
        first = fixture.imports[1]
        delivery = str(fixture.store.get_record("import_batch", first)["delivery_id"])
        scoped = fixture.store.add_identifier(
            fixture.packages[0],
            namespace="dk:varenummer",
            identifier_value="7",
            evidence_import_id=first,
            source_locator="record:1",
            mapping_version="quoted-local",
            validity_kind="scope",
            scope_key=f"delivery:{delivery}",
            available=timing(),
        )
        selected = results(fixture, fixture.config(mapping_ids=[*fixture.mappings, scoped]))
    else:
        selected = results(fixture)
    assert len(selected) == 2
    assert {item["value"] for item in selected} == {100, 900}
    assert all(item["usable"] for item in selected)


def test_snapshot_omission_does_not_resurrect_old_row(fixture: Fixture) -> None:
    old = fixture.source(
        "old",
        [{"amount_ore": 100}, {"amount_ore": 200, "package_index": 1}],
        kind="complete_snapshot",
    )
    new = fixture.source("new", [{"amount_ore": 300}], kind="complete_snapshot")
    evidence = fixture.operation("replacement", new, [old], kind="complete_snapshot")
    selected = results(fixture, fixture.config(revisions=[evidence]))
    assert {item["key"]["package_id"]: item["value"] for item in selected} == {
        fixture.packages[0]: 300,
        fixture.packages[1]: None,
    }
    omitted = next(item for item in selected if item["key"]["package_id"] == fixture.packages[1])
    assert omitted["state"] == "missing"
    assert omitted["assertions"][0]["suppressed"] == "superseded_in_declared_scope"


@pytest.mark.parametrize("failure", ["missing_member", "rejected", "partial", "bad_inventory"])
def test_incomplete_revision_preserves_prior_evidence(fixture: Fixture, failure: str) -> None:
    old = fixture.source("old", [{"amount_ore": 100}], kind="complete_snapshot")
    new = fixture.source(
        "new",
        [{"amount_ore": 300}],
        kind="partial_extract" if failure == "partial" else "complete_snapshot",
        rejected=int(failure == "rejected"),
    )
    changes = {}
    if failure == "missing_member":
        changes["expected_members"] = ["main", "missing"]
    if failure == "bad_inventory":
        changes["expected_observation_ids"] = []
    evidence = fixture.operation("replacement", new, [old], kind="complete_snapshot", **changes)
    selection = fixture.store.create_selection(fixture.config(revisions=[evidence]))
    assert selection["evidence"]["result"][0]["value"] == 100
    assert any(item.get("status") == "unresolved" for item in selection["evidence"]["coverage"])


@pytest.mark.parametrize(
    "state,expected",
    [
        ("present", "present"),
        ("blank", "blank"),
        ("withdrawn", "withdrawn"),
        ("missing", "present"),
        ("unreported", "present"),
    ],
)
def test_field_patch_preserves_unrelated_fields_and_missingness(
    fixture: Fixture,
    state: str,
    expected: str,
) -> None:
    old = fixture.source(
        "old",
        [
            {
                "table": "package_observation",
                "name": "Original name",
                "company": "Original company",
            }
        ],
    )
    new = fixture.source(
        "new",
        [
            {
                "table": "package_observation",
                "company": "New company" if state == "present" else None,
                "presence": {"company": state},
            }
        ],
        kind="patch",
    )
    op = fixture.operation(
        "patch", new, [old], kind="patch", table="package_observation", fields=["company"]
    )
    selected = results(fixture, fixture.config(revisions=[op]))
    by_field = {item["key"]["field"]: item for item in selected}
    assert by_field["name"]["value"] == "Original name"
    assert by_field["company"]["state"] == expected
    assert by_field["company"]["value"] == (
        "New company"
        if state == "present"
        else "Original company"
        if state in {"missing", "unreported"}
        else None
    )


def test_change_feed_updates_only_explicit_keys(fixture: Fixture) -> None:
    old = fixture.source("old", [{"amount_ore": 100}, {"amount_ore": 200, "package_index": 1}])
    new = fixture.source("new", [{"amount_ore": 300}], kind="changes")
    op = fixture.operation("change", new, [old], kind="changes")
    selected = results(fixture, fixture.config(revisions=[op]))
    assert sorted(item["value"] for item in selected) == [200, 300]


def test_partial_extract_without_revision_never_implies_omission(fixture: Fixture) -> None:
    fixture.source("old", [{"amount_ore": 100}, {"amount_ore": 200, "package_index": 1}])
    fixture.source("new", [{"amount_ore": 100}], kind="partial_extract")
    assert sorted(item["value"] for item in results(fixture)) == [100, 200]


def test_historical_attributes_are_not_backdated(fixture: Fixture) -> None:
    fixture.source(
        "old",
        [
            {
                "table": "package_observation",
                "company": "New company",
                "effective_period": NEXT,
            },
            {"amount_ore": 100, "effective_period": PERIOD},
        ],
    )
    config = fixture.config()
    config["scope"]["periods"] = [PERIOD]
    selected = results(fixture, config)
    assert len(selected) == 1 and selected[0]["key"]["table"] == "price_observation"


def test_classification_group_contexts_are_coherent_and_separate(fixture: Fixture) -> None:
    rows = [
        {
            "table": "classification_observation",
            "classification_context": context,
            "abc_code": "A",
            "group_namespace": "issuer",
            "group_code": "1",
            "group_scope": PERIOD,
        }
        for context in ("ordinary", "dose")
    ]
    rows.append(
        {
            "table": "classification_observation",
            "classification_context": "ordinary",
            "abc_code": "C",
            "effective_period": NEXT,
            "group_namespace": "issuer",
        }
    )
    fixture.source("classifications", rows)
    selected = results(fixture)
    groups = [item for item in selected if item["key"]["field"] == "group" and item["usable"]]
    assert len(groups) == 2
    assert all(
        item["value"]
        == {
            "group_namespace": "issuer",
            "group_scope": PERIOD,
            "group_code": "1",
        }
        for item in groups
    )
    upcoming = [item for item in selected if item["key"]["classification_phase"] == "upcoming"]
    assert [item["value"] for item in upcoming if item["usable"]] == ["C"]


def test_supply_is_dated_and_unknown_lifecycle_is_excluded(fixture: Fixture) -> None:
    fixture.source(
        "events",
        [
            {
                "table": "supply_observation",
                "observation_date": PERIOD,
                "wholesaler_namespace": "invented",
                "wholesaler_identifier": "1",
                "reported_status": "reported_disruption",
            },
            {
                "table": "lifecycle_event",
                "effective_date": PERIOD,
                "event_type": "deactivation",
                "original_source_code": "stop",
                "interpretation_status": "unknown",
                "quality_state": "incomplete",
            },
        ],
    )
    selected = results(fixture)
    assert len(selected) == 1
    assert selected[0]["key"]["date"] == PERIOD
    assert selected[0]["value"] == "reported_disruption"


def test_unverified_assertions_cannot_enter_analytical_use(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    selection = fixture.store.create_selection(fixture.config(verification_reports=[]))
    assert not selection["evidence"]["result"]
    assert {item["reason"] for item in selection["evidence"]["exclusions"]} == {
        "not_independently_verified"
    }


def test_source_preference_is_versioned_scoped_and_evidenced(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    fixture.source("two", [{"amount_ore": 300}])
    evidence = fixture.document("authority", {"meaning": "one preferred for this synthetic field"})
    config = fixture.config(
        source_preference={
            "version": "explicit-v1",
            "rules": [
                {
                    "scope": {
                        "tables": ["price_observation"],
                        "fields": ["amount_ore"],
                        "periods": [PERIOD],
                    },
                    "sources": ["one", "two"],
                    "evidence_delivery_id": evidence,
                    "source_locator": "meaning",
                }
            ],
        }
    )
    selected = results(fixture, config)[0]
    assert selected["value"] == 100
    assert len(selected["assertions"]) == 2
    assert len(selected["supporting_observation_ids"]) == 1


def test_frozen_selection_stable_after_backfill_and_correction_and_restore(
    tmp_path: Path,
) -> None:
    database, archive = tmp_path / "store.duckdb", tmp_path / "originals"
    with Store(database, archive) as store:
        fixture = Fixture(store, tmp_path)
        fixture.source("first", [{"amount_ore": 100}])
        config = fixture.config()
        frozen = store.create_selection(config)
        fixture.source("backfill", [{"amount_ore": 900}])
        assert store.create_selection(config) == frozen
        assert store.inspect_selection(frozen["selection_id"]) == frozen
        changed = store.create_selection(fixture.config())
        assert changed["selection_id"] != frozen["selection_id"]
        assert changed["evidence"]["result"][0]["state"] == "conflict"
    backup_store(database, archive, tmp_path / "backup")
    restore_store(
        tmp_path / "backup", tmp_path / "restored.duckdb", tmp_path / "restored-originals"
    )
    with Store(tmp_path / "restored.duckdb", tmp_path / "restored-originals") as store:
        assert store.inspect_selection(frozen["selection_id"]) == frozen
        assert store.create_selection(config) == frozen


def test_failed_selection_transaction_publishes_no_partial_artifact(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    insert = fixture.store._insert

    def fail(table: str, record: dict[str, Any]) -> None:
        insert(table, record)
        if table == "frozen_selection":
            raise RuntimeError("synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(fixture.store, "_insert", fail)
        with pytest.raises(RuntimeError, match="interruption"):
            fixture.store.create_selection(fixture.config())
    assert fixture.store._connection.execute(
        "SELECT count(*) FROM frozen_selection"
    ).fetchone() == (0,)
    assert results(fixture)[0]["value"] == 100


def test_f2_migration_preserves_every_record_and_rolls_back_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bef_redbird.foundation import migrations

    full = migrations.MIGRATIONS
    database, archive = tmp_path / "store.duckdb", tmp_path / "originals"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", full[:2])
        with Store(database, archive) as store:
            fixture = Fixture(store, tmp_path)
            fixture.source("one", [{"amount_ore": 100}])
            tables = [row[0] for row in store._connection.execute("SHOW TABLES").fetchall()]
            baseline = {
                table: serial(
                    store._connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
                )
                for table in tables
            }
    backup_store(database, archive, tmp_path / "backup-f2")
    bad = tmp_path / "bad.sql"
    bad.write_text("CREATE TABLE unintended(i INTEGER); SELECT unknown_column;", encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", (*full[:2], (3, "bad", bad)))
        with pytest.raises(duckdb.Error):
            with Store(database, archive):
                pass
    with duckdb.connect(str(database), read_only=True) as connection:
        connection.execute("SET TimeZone='UTC'")
        assert not connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='unintended'"
        ).fetchall()
        for table in tables:
            assert (
                serial(connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall())
                == baseline[table]
            )
    with Store(database, archive) as store:
        for table in tables:
            query = (
                f"SELECT * FROM {table}"
                + (" WHERE version <= 2" if table == "schema_migration" else "")
                + " ORDER BY ALL"
            )
            assert serial(store._connection.execute(query).fetchall()) == baseline[table]
        assert store._connection.execute("SELECT count(*) FROM frozen_selection").fetchone() == (0,)
    restore_store(
        tmp_path / "backup-f2", tmp_path / "rollback.duckdb", tmp_path / "rollback-originals"
    )
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", full[:2])
        with Store(tmp_path / "rollback.duckdb", tmp_path / "rollback-originals") as store:
            for table in tables:
                assert (
                    serial(
                        store._connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
                    )
                    == baseline[table]
                )


def test_frozen_hash_tampering_is_detected(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    frozen = fixture.store.create_selection(fixture.config())
    fixture.store._connection.execute("UPDATE frozen_selection SET evidence = '{}'")
    with pytest.raises(FoundationError, match="integrity"):
        fixture.store.inspect_selection(frozen["selection_id"])


def test_missing_original_blocks_inspection(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    frozen = fixture.store.create_selection(fixture.config())
    original = fixture.store.original_path(next(iter(frozen["configuration"]["derivations"])))
    original.unlink()
    with pytest.raises(FoundationError):
        fixture.store.inspect_selection(frozen["selection_id"])


def test_unknown_configuration_semantics_are_rejected(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    config = fixture.config()
    wrong = copy.deepcopy(config)
    wrong["latest_wins"] = True
    with pytest.raises(ValueError):
        fixture.store.create_selection(wrong)
    wrong = copy.deepcopy(config)
    wrong["cutoff"] = "2026-01-14T12:00:00"
    with pytest.raises(ValueError, match="timezone"):
        fixture.store.create_selection(wrong)


def test_excluded_new_row_cannot_make_replacement_look_complete(fixture: Fixture) -> None:
    old = fixture.source(
        "old",
        [
            {"amount_ore": 100},
            {"amount_ore": 200, "package_index": 1},
        ],
        kind="complete_snapshot",
    )
    new = fixture.source(
        "new",
        [
            {"amount_ore": 300},
            {"amount_ore": 400, "package_index": 1},
        ],
        kind="complete_snapshot",
    )
    new_rows = [row for row in fixture.rows if row["import_id"] == new]
    omitted_id = new_rows[1]["observation_id"]
    op = fixture.operation(
        "incomplete-inventory",
        new,
        [old],
        kind="complete_snapshot",
        expected_observation_ids=[new_rows[0]["observation_id"]],
    )
    config = fixture.config(revisions=[op])
    report = json.loads(fixture.store.original_path(config["verification_reports"][0]).read_text())
    report["verified_observation_ids"].remove(omitted_id)
    report["observation_hashes"].pop(omitted_id)
    config["verification_reports"] = [fixture.document("incomplete-audit", report)]
    frozen = fixture.store.create_selection(config)
    assert sorted(item["value"] for item in frozen["evidence"]["result"]) == [100, 200]
    assert any(
        item.get("reason") == "revision_assertion_inventory_mismatch"
        for item in frozen["evidence"]["coverage"]
    )


def test_partial_release_member_prevents_replacement(fixture: Fixture) -> None:
    old = fixture.source("old", [{"amount_ore": 100}], kind="complete_snapshot")
    new = fixture.source("new", [{"amount_ore": 300}], kind="complete_snapshot", source="release")
    member = fixture.source(
        "member",
        [{"amount_ore": 400, "package_index": 1}],
        kind="partial_extract",
        source="release",
    )
    op = fixture.operation(
        "mixed-release",
        new,
        [old],
        kind="complete_snapshot",
        expected_members=["main", "other"],
        members={"main": new, "other": member},
        expected_observation_ids=[
            row["observation_id"] for row in fixture.rows if row["import_id"] in {new, member}
        ],
    )
    frozen = fixture.store.create_selection(fixture.config(revisions=[op]))
    usable = [item for item in frozen["evidence"]["result"] if item["usable"]]
    assert [item["value"] for item in usable] == [100]
    assert any(
        item.get("reason") == "revision_coverage_unestablished"
        for item in frozen["evidence"]["coverage"]
    )


def test_unknown_monetary_bases_are_not_claimed_comparable(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100, "tax_basis": "unknown"}])
    fixture.source("two", [{"amount_ore": 100, "tax_basis": "unknown"}])
    selected = results(fixture)
    assert len(selected) == 2
    assert all(item["value"] == 100 and not item["usable"] for item in selected)
    assert all(item["reason"] == "unknown_price_basis" for item in selected)
    assert all(len(item["supporting_observation_ids"]) == 1 for item in selected)


def test_equivalent_lifecycle_events_retain_different_source_codes(fixture: Fixture) -> None:
    for label in ("source-a", "source-b"):
        fixture.source(
            label,
            [
                {
                    "table": "lifecycle_event",
                    "effective_period": None,
                    "effective_date": PERIOD,
                    "event_type": "introduction",
                    "original_source_code": label,
                    "interpretation_status": "established",
                }
            ],
        )
    selected = results(fixture)
    assert len(selected) == 1
    assert selected[0]["value"] == "introduction"
    assert selected[0]["usable"] is True
    assert len(selected[0]["supporting_observation_ids"]) == 2


def test_reinterpretation_still_verifies_original_mapping_evidence(fixture: Fixture) -> None:
    first = fixture.source("one", [{"amount_ore": 100}])
    later_document = fixture.document("alternate-identity", {"package": "same synthetic package"})
    later_import = fixture.store.begin_import(
        later_document,
        parser_version="mapping-v2",
        schema_version="mapping-v2",
        mapping_version="mapping-v2",
    )
    fixture.store.finish_import(
        later_import, status="accepted", record_count=1, accepted_count=1, rejected_count=0
    )
    alternate = fixture.store.add_identifier(
        fixture.packages[0],
        namespace="dk:varenummer",
        identifier_value="7",
        evidence_import_id=later_import,
        source_locator="package",
        mapping_version="mapping-v2",
        validity_kind="interval",
        valid_from=date(2026, 1, 5),
        valid_to=date(2026, 2, 2),
        available=timing(),
    )
    config = fixture.config(mapping_ids=[alternate])
    frozen = fixture.store.create_selection(config)
    original = fixture.store.get_record("package_identifier", fixture.mappings[0])
    assert fixture.mappings[0] in frozen["evidence"]["inputs"]["original_mappings"]
    original_batch = fixture.store.get_record("import_batch", str(original["evidence_import_id"]))
    fixture.store.original_path(str(original_batch["delivery_id"])).unlink()
    # Different policy config forces a new freeze; original evidence is still mandatory.
    config["business_context"] = {"purpose": "another attempt after original evidence loss"}
    with pytest.raises(FoundationError):
        fixture.store.create_selection(config)
    assert first in config["import_ids"]


def test_late_interpretation_evidence_cannot_leak_into_strict_selection(fixture: Fixture) -> None:
    batch = fixture.source("one", [{"amount_ore": 100}])
    evidence = fixture.document("late-meaning", {"tax": "defined now"}, received=timing(FRIDAY))
    config = fixture.config(context_evidence={batch: [evidence]})
    assert results(fixture, config) == []


def test_live_completion_crossing_deadline_is_late(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bef_redbird.foundation.selection as selection

    fixture.source("one", [{"amount_ore": 100}])
    moments = iter(
        [
            datetime.fromisoformat("2026-01-14T11:59:59+00:00"),
            datetime.fromisoformat("2026-01-14T12:00:01+00:00"),
        ]
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(moments)

    config = fixture.config(mode="live")
    monkeypatch.setattr(selection, "datetime", Clock)
    frozen = fixture.store.create_selection(config)
    assert frozen["evidence"]["late"] is True
    assert datetime.fromisoformat(frozen["produced_at"]).second == 1


def test_stale_verification_hash_excludes_modified_record(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    config = fixture.config()
    fixture.store._connection.execute("UPDATE price_observation SET amount_ore=101")
    frozen = fixture.store.create_selection(config)
    assert frozen["evidence"]["result"] == []
    assert any(
        item["reason"] == "not_independently_verified" for item in frozen["evidence"]["exclusions"]
    )


def test_skipped_physical_row_blocks_snapshot_completeness(fixture: Fixture) -> None:
    old = fixture.source(
        "old",
        [{"amount_ore": 100}, {"package_index": 1, "amount_ore": 200}],
        kind="complete_snapshot",
    )
    new = fixture.source(
        "new",
        [{"amount_ore": 300}],
        kind="complete_snapshot",
        issues=[
            {
                "source_locator": "record:2",
                "identifier": "malformed",
                "code": "invalid_identifier",
                "severity": "error",
                "blocking_scope": "identifier",
            }
        ],
    )
    operation = fixture.operation("skipped-row", new, [old], kind="complete_snapshot")
    frozen = fixture.store.create_selection(fixture.config(revisions=[operation]))
    assert sorted(item["value"] for item in frozen["evidence"]["result"]) == [100, 200]
    assert any(
        "revision_unresolved_source_rows" in item.get("reasons", [])
        for item in frozen["evidence"]["coverage"]
    )


def test_repeated_member_alias_cannot_fake_complete_release(fixture: Fixture) -> None:
    old = fixture.source("old", [{"amount_ore": 100}], kind="complete_snapshot")
    new = fixture.source("new", [{"amount_ore": 300}], kind="complete_snapshot")
    operation = fixture.operation(
        "aliased-release",
        new,
        [old],
        kind="complete_snapshot",
        expected_members=["main", "other"],
        members={"main": new, "other": new},
    )
    frozen = fixture.store.create_selection(fixture.config(revisions=[operation]))
    assert frozen["evidence"]["result"][0]["value"] == 100
    assert any(
        "incomplete_release" in item.get("reasons", []) for item in frozen["evidence"]["coverage"]
    )


@pytest.mark.parametrize(
    "column,value",
    [
        ("availability_mode", "reconstructed_research"),
        ("policy_version", "unreviewed-policy"),
        ("produced_at", "2026-01-14T12:00:00Z"),
        ("decision_cutoff", "2026-01-17T12:00:00Z"),
    ],
)
def test_frozen_scalar_metadata_is_integrity_checked(
    fixture: Fixture,
    column: str,
    value: str,
) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    frozen = fixture.store.create_selection(fixture.config())
    fixture.store._connection.execute(f"UPDATE frozen_selection SET {column} = ?", [value])
    with pytest.raises(FoundationError, match="metadata"):
        fixture.store.inspect_selection(frozen["selection_id"])


def test_patch_scope_does_not_touch_other_period_or_price_type(fixture: Fixture) -> None:
    old = fixture.source(
        "old",
        [
            {"amount_ore": 100},
            {"amount_ore": 200, "price_type": "registered"},
            {"amount_ore": 300, "effective_period": NEXT},
        ],
    )
    new = fixture.source("new", [{"amount_ore": 900}], kind="patch")
    op_scope = {
        "package_ids": fixture.packages,
        "periods": [PERIOD],
        "dates": [],
        "tables": ["price_observation"],
        "fields": ["amount_ore"],
        "context": {"price_type": ["aip"]},
    }
    operation = fixture.operation("limited-patch", new, [old], kind="patch", scope=op_scope)
    selected = results(fixture, fixture.config(revisions=[operation]))
    assert sorted(item["value"] for item in selected) == [200, 300, 900]


def test_revision_cycles_are_rejected(fixture: Fixture) -> None:
    first = fixture.source("first", [{"amount_ore": 100}], kind="complete_snapshot")
    second = fixture.source("second", [{"amount_ore": 300}], kind="complete_snapshot")
    one = fixture.operation("cycle-one", first, [second], kind="complete_snapshot")
    two = fixture.operation("cycle-two", second, [first], kind="complete_snapshot")
    with pytest.raises(ValueError, match="Cyclic"):
        fixture.store.create_selection(fixture.config(revisions=[one, two]))


@pytest.mark.parametrize(
    "failure",
    [
        {"complete": False},
        {"mismatches": [{"kind": "mapping_all_fields"}]},
        {"quarantined_observation_ids": [uid("quarantined")]},
    ],
)
def test_failed_global_audit_cannot_admit_locally_verified_rows(
    fixture: Fixture,
    failure: dict[str, Any],
) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    config = fixture.config()
    report = json.loads(fixture.store.original_path(config["verification_reports"][0]).read_text())
    report.update(failure)
    config["verification_reports"] = [fixture.document("failed-audit", report)]
    with pytest.raises(ValueError, match="incomplete or failed"):
        fixture.store.create_selection(config)


def test_unverified_mapping_cannot_join_verified_prices(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    config = fixture.config()
    report = json.loads(fixture.store.original_path(config["verification_reports"][0]).read_text())
    report["mapping_hashes"] = {}
    config["verification_reports"] = [fixture.document("no-mapping-verification", report)]
    frozen = fixture.store.create_selection(config)
    assert frozen["evidence"]["result"] == []


def test_undated_history_attributes_do_not_block_dated_prices(fixture: Fixture) -> None:
    fixture.source(
        "one",
        [
            {"amount_ore": 100},
            {"table": "package_observation", "effective_period": None, "name": "Synthetic package"},
        ],
        issues=[
            {
                "source_locator": "record:1",
                "identifier": "000007",
                "code": "unknown_attribute_period",
                "severity": "error",
                "blocking_scope": "field",
                "field": "effective_period",
            }
        ],
    )
    selected = results(fixture, fixture.config())
    assert len(selected) == 1
    assert selected[0]["value"] == 100


@pytest.mark.parametrize("late_support", [False, True])
@pytest.mark.parametrize("version", [1, 2])
def test_bounded_mapping_document_enforces_delivery_scope_and_support_timing(
    fixture: Fixture,
    late_support: bool,
    version: int,
) -> None:
    one = fixture.source(
        "one", [{"amount_ore": 100}, {"amount_ore": 300, "effective_period": NEXT}]
    )
    fixture.source("outside", [{"amount_ore": 200}])
    supporting = fixture.document(
        "support",
        {"meaning": "synthetic evidence"},
        received=timing(FRIDAY if late_support else MONDAY),
    )
    evidence = fixture.document(
        "bounded-mapping",
        {
            "allowed_delivery_ids": [
                str(fixture.store.get_record("import_batch", one)["delivery_id"])
            ],
            "supporting_delivery_ids": [supporting],
            "entries": [
                {
                    "source_locator": "entry:1",
                    "package_id": fixture.packages[0],
                    "namespace": "dk:varenummer",
                    "canonical_identifier": "000007",
                    # The same receipt's other period is expressly outside this evidence.
                    "supporting_observation_ids": [fixture.rows[0]["observation_id"]],
                }
            ],
        },
    )
    batch = fixture.store.begin_import(
        evidence,
        parser_version="identity-test-v1",
        schema_version="f3-identity-evidence-v1",
        mapping_version="bounded-v1",
    )
    fixture.store.finish_import(
        batch, status="accepted", record_count=1, accepted_count=1, rejected_count=0
    )
    mapping = fixture.store.add_identifier(
        fixture.packages[0],
        namespace="dk:varenummer",
        identifier_value="7",
        evidence_import_id=batch,
        source_locator="entry:1",
        mapping_version="bounded-v1",
        validity_kind="scope",
        scope_key="bounded-test",
        available=timing(),
    )
    configure = fixture.config if version == 1 else lambda **kw: catalogue_config(fixture, **kw)
    selected = results(fixture, configure(mapping_ids=[mapping], identity_scope="bounded-test"))
    assert [row["value"] for row in selected] == ([] if late_support else [100])


def catalogue_config(fixture: Fixture, **changes: Any) -> dict[str, Any]:
    return fixture.config(
        **{
            "version": 2,
            "candidate_catalogue": [
                {"namespace": "dk:varenummer", "canonical_identifier": "000007"}
            ],
            "mapping_ids": [fixture.mappings[0]],
            **changes,
        }
    )


def extra_mapping(fixture: Fixture, package: str | None, *, available: Timing | None = None) -> str:
    original = fixture.store.get_record("package_identifier", fixture.mappings[0])
    return fixture.store.add_identifier(
        package,
        namespace="dk:varenummer",
        identifier_value="7",
        evidence_import_id=str(original["evidence_import_id"]),
        source_locator=f"explicit-alternative:{package}",
        mapping_version="explicit-alternative-v1",
        validity_kind="interval",
        valid_from=date(2026, 1, 5),
        valid_to=date(2026, 2, 2),
        available=available if available is not None else timing(),
    )


def replace_verification(
    fixture: Fixture, config: dict[str, Any], report: dict[str, Any], *, compressed: bool = False
) -> None:
    if compressed:
        path = fixture.root / f"proof-{fixture.report_sequence}.json.gz"
        path.write_bytes(gzip.compress(json.dumps(report).encode(), mtime=0))
        delivery = fixture.store.register_source(
            path, source="gzip-proof", receipt_key=path.name, received=timing()
        )
    else:
        delivery = fixture.document(f"changed-proof-{fixture.report_sequence}", report)
    config["verification_reports"] = [delivery]


def test_catalogue_retains_all_candidate_fields_and_periods_with_bounded_hashes(
    fixture: Fixture,
) -> None:
    batch = fixture.source(
        "catalogue",
        [
            {"amount_ore": 1255},
            {"amount_ore": 999, "effective_period": NEXT},
            {"table": "package_observation", "name": "Explicit package"},
            *[{"package_index": 1, "amount_ore": 400} for _ in range(100)],
        ],
    )
    config = catalogue_config(fixture)
    config["scope"].update(
        tables=["price_observation"], periods=[PERIOD], dates=[], fields=["amount_ore"]
    )
    selector = CatalogueSelector(fixture.store, _configuration(config))
    evidence = selector.run()
    assert evidence["version"] == 2
    assert [(row["value"], row["usable"]) for row in evidence["result"]] == [(1255, True)]
    retained = {row["observation_id"] for row in fixture.rows[:3]}
    assert set(evidence["inputs"]["observations"]) == retained
    assert set(selector.verified) == retained
    assert set(selector.verified_mappings) == {fixture.mappings[0]}
    assert len(evidence["inputs"]["original_mappings"]) == 1
    assert len(evidence["exclusions"]) == 8
    assert {item["reason"] for item in evidence["exclusions"]} == {"outside_business_scope"}
    inventory = evidence["input_retention"]["inventory"]
    assert next(item for item in inventory if item["table"] == "price_observation") == {
        "import_id": batch,
        "table": "price_observation",
        "total": 102,
        "retained": 2,
        "omitted": 100,
    }
    assert "not individually frozen" in evidence["input_retention"]["omitted_rows"]


def test_catalogue_queries_identifiers_before_alternative_package_identity(
    fixture: Fixture,
) -> None:
    fixture.source("aliases", [{"amount_ore": 100}, {"package_index": 1, "amount_ore": 100}])
    alternate = extra_mapping(fixture, fixture.packages[1])
    config = catalogue_config(
        fixture,
        mapping_ids=[alternate, fixture.mappings[1]],
        candidate_catalogue=[
            {"namespace": "dk:varenummer", "canonical_identifier": "000008"},
            {"namespace": "dk:varenummer", "canonical_identifier": "000007"},
        ],
    )
    config["scope"]["package_ids"] = [fixture.packages[1]]
    frozen = fixture.store.create_selection(config)
    selected = frozen["evidence"]["result"]
    assert len(selected) == 1
    assert selected[0]["value"] == 100
    assert selected[0]["key"]["package_id"] == fixture.packages[1]
    assert len(selected[0]["supporting_observation_ids"]) == 2
    original = frozen["evidence"]["inputs"]["observations"][fixture.rows[0]["observation_id"]]
    assert original["record"]["package_id"] == fixture.packages[0]
    assert original["record"]["mapping_id"] == fixture.mappings[0]
    assert [p["canonical_identifier"] for p in frozen["configuration"]["candidate_catalogue"]] == [
        "000007",
        "000008",
    ]


@pytest.mark.parametrize("kind", ["late_conflict", "unknown_time_conflict", "unknown_package"])
def test_catalogue_cannot_filter_away_competing_identity(fixture: Fixture, kind: str) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    competing = extra_mapping(
        fixture,
        None if kind == "unknown_package" else fixture.packages[1],
        available=timing(None if kind == "unknown_time_conflict" else FRIDAY),
    )
    config = catalogue_config(fixture, mapping_ids=[fixture.mappings[0], competing])
    frozen = fixture.store.create_selection(config)
    assert frozen["evidence"]["result"] == []
    assert any(
        item["reason"] == "unresolved_or_unavailable_identity"
        for item in frozen["evidence"]["exclusions"]
    )


@pytest.mark.parametrize("failure", ["unverified", "stale", "original_mapping", "selected_mapping"])
def test_catalogue_exact_candidate_verification_gates(fixture: Fixture, failure: str) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    alternate = extra_mapping(fixture, fixture.packages[0])
    config = catalogue_config(fixture, mapping_ids=[alternate])
    report = json.loads(fixture.store.original_path(config["verification_reports"][0]).read_bytes())
    oid = fixture.rows[0]["observation_id"]
    if failure == "unverified":
        report["verified_observation_ids"].remove(oid)
        del report["observation_hashes"][oid]
    elif failure == "stale":
        fixture.store._connection.execute(
            "UPDATE price_observation SET amount_ore=999 WHERE observation_id=?", [oid]
        )
    else:
        del report["mapping_hashes"][
            fixture.mappings[0] if failure == "original_mapping" else alternate
        ]
    replace_verification(fixture, config, report)
    assert results(fixture, config) == []


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("failure", [None, "mismatch", "quarantine", "bad_omitted_hash"])
def test_catalogue_plain_and_gzip_proof_gates(
    fixture: Fixture, compressed: bool, failure: str | None
) -> None:
    fixture.source("two", [{"amount_ore": 1255}, {"package_index": 1, "amount_ore": 400}])
    config = catalogue_config(fixture)
    report = json.loads(fixture.store.original_path(config["verification_reports"][0]).read_bytes())
    omitted = fixture.rows[1]["observation_id"]
    if failure == "mismatch":
        report["mismatches"] = [{"observation_id": omitted, "field": "amount_ore"}]
    elif failure == "quarantine":
        report["quarantined_observation_ids"] = [omitted]
    elif failure == "bad_omitted_hash":
        report["observation_hashes"][omitted] = "not-a-hash"
    replace_verification(fixture, config, report, compressed=compressed)
    if failure:
        with pytest.raises(ValueError, match="incomplete or failed|Invalid observation"):
            results(fixture, config)
    else:
        selected = results(fixture, config)
        assert [(item["value"], item["usable"]) for item in selected] == [(1255, True)]


def test_catalogue_omitted_rows_are_not_claimed_frozen_but_candidate_tampering_is_detected(
    fixture: Fixture,
) -> None:
    fixture.source("two", [{"amount_ore": 100}, {"package_index": 1, "amount_ore": 200}])
    legacy = fixture.store.create_selection(fixture.config())
    frozen = fixture.store.create_selection(catalogue_config(fixture))
    fixture.store._connection.execute(
        "UPDATE price_observation SET amount_ore=201 WHERE observation_id=?",
        [fixture.rows[1]["observation_id"]],
    )
    assert fixture.store.inspect_selection(frozen["selection_id"]) == frozen
    with pytest.raises(FoundationError, match="Frozen source assertion changed"):
        fixture.store.inspect_selection(legacy["selection_id"])
    fixture.store._connection.execute(
        "UPDATE price_observation SET amount_ore=101 WHERE observation_id=?",
        [fixture.rows[0]["observation_id"]],
    )
    with pytest.raises(FoundationError, match="Frozen source assertion changed"):
        fixture.store.inspect_selection(frozen["selection_id"])


def test_catalogue_count_drift_is_detected_without_freezing_omitted_rows(fixture: Fixture) -> None:
    fixture.source("two", [{"amount_ore": 100}, {"package_index": 1, "amount_ore": 200}])
    frozen = fixture.store.create_selection(catalogue_config(fixture))
    fixture.store._connection.execute(
        "DELETE FROM price_observation WHERE observation_id=?", [fixture.rows[1]["observation_id"]]
    )
    with pytest.raises(FoundationError, match="Frozen candidate inventory changed"):
        fixture.store.inspect_selection(frozen["selection_id"])


@pytest.mark.parametrize(
    "catalogue",
    [
        [],
        None,
        [{"namespace": "dk:varenummer", "canonical_identifier": "7"}],
        [{"namespace": "dk:varenummer", "canonical_identifier": 7}],
        [{"namespace": "", "canonical_identifier": "000007"}],
        [{"namespace": "dk:varenummer", "canonical_identifier": "000007"}] * 2,
        [{"namespace": "dk:varenummer", "canonical_identifier": "000007", "implicit": True}],
    ],
)
def test_catalogue_requires_explicit_unique_canonical_pairs(
    fixture: Fixture, catalogue: Any
) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    with pytest.raises(ValueError):
        fixture.store.create_selection(catalogue_config(fixture, candidate_catalogue=catalogue))


def test_catalogue_rejects_revision_and_outside_mapping_contracts(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    with pytest.raises(ValueError, match="do not support revision"):
        fixture.store.create_selection(catalogue_config(fixture, revisions=[uid("revision")]))
    with pytest.raises(ValueError, match="mapping is outside"):
        fixture.store.create_selection(catalogue_config(fixture, mapping_ids=fixture.mappings))
    legacy = fixture.config()
    legacy["candidate_catalogue"] = [
        {"namespace": "dk:varenummer", "canonical_identifier": "000007"}
    ]
    with pytest.raises(ValueError, match="unsupported fields"):
        fixture.store.create_selection(legacy)


def test_catalogue_missing_selected_mapping_does_not_erase_candidate(fixture: Fixture) -> None:
    fixture.source("one", [{"amount_ore": 100}])
    frozen = fixture.store.create_selection(catalogue_config(fixture, mapping_ids=[]))
    assert frozen["evidence"]["result"] == []
    assert set(frozen["evidence"]["inputs"]["observations"]) == {fixture.rows[0]["observation_id"]}
    assert any(
        item["reason"] == "unresolved_or_unavailable_identity"
        for item in frozen["evidence"]["exclusions"]
    )
    with pytest.raises(ValueError):
        fixture.store.create_selection(
            catalogue_config(fixture, mapping_ids=[uid("nonexistent-mapping")])
        )


def test_catalogue_pairs_do_not_form_namespace_identifier_cross_product(fixture: Fixture) -> None:
    fixture.source("two", [{"amount_ore": 100}, {"package_index": 1, "amount_ore": 200}])
    frozen = fixture.store.create_selection(
        catalogue_config(
            fixture,
            candidate_catalogue=[
                {"namespace": "dk:varenummer", "canonical_identifier": "000007"},
                {"namespace": "other", "canonical_identifier": "000008"},
            ],
        )
    )
    assert set(frozen["evidence"]["inputs"]["observations"]) == {fixture.rows[0]["observation_id"]}
    assert [item["value"] for item in frozen["evidence"]["result"]] == [100]


@pytest.mark.parametrize("mode", ["historical_replay", "reconstructed_research"])
def test_catalogue_unknown_timing_and_price_basis_stay_explicit(
    fixture: Fixture, mode: str
) -> None:
    fixture.source("unknown", [{"amount_ore": 100, "tax_basis": "unknown"}], received=Timing())
    frozen = fixture.store.create_selection(catalogue_config(fixture, mode=mode))
    evidence = frozen["evidence"]
    if mode == "historical_replay":
        assert evidence["result"] == []
        assert any(item["reason"] == "unknown_availability" for item in evidence["exclusions"])
    else:
        assert len(evidence["result"]) == 1
        assert evidence["result"][0]["value"] == 100
        assert evidence["result"][0]["usable"] is False
        assert evidence["result"][0]["reason"] == "unknown_price_basis"
        assert evidence["availability_disclosures"]


@pytest.mark.parametrize("duplicate", [False, True])
def test_catalogue_cached_identity_entries_preserve_duplicate_rejection_and_support(
    fixture: Fixture, duplicate: bool
) -> None:
    batch = fixture.source(
        "two-periods", [{"amount_ore": 100}, {"amount_ore": 200, "effective_period": NEXT}]
    )
    source_id = str(fixture.store.get_record("import_batch", batch)["delivery_id"])
    entry = {
        "source_locator": "entries:0",
        "package_id": fixture.packages[0],
        "namespace": "dk:varenummer",
        "canonical_identifier": "000007",
        "supporting_observation_ids": [row["observation_id"] for row in fixture.rows],
    }
    delivery = fixture.document(
        "bounded-cache",
        {
            "allowed_delivery_ids": [source_id],
            "supporting_delivery_ids": [source_id],
            "entries": [entry, entry] if duplicate else [entry],
        },
    )
    evidence_import = fixture.store.begin_import(
        delivery,
        parser_version="identity-test-v1",
        schema_version="f3-identity-evidence-v1",
        mapping_version="cache-test",
    )
    fixture.store.finish_import(
        evidence_import, status="accepted", record_count=1, accepted_count=1, rejected_count=0
    )
    mapping = fixture.store.add_identifier(
        fixture.packages[0],
        namespace="dk:varenummer",
        identifier_value="7",
        evidence_import_id=evidence_import,
        source_locator="entries:0",
        mapping_version="cache-test",
        validity_kind="scope",
        scope_key="bounded-cache",
        available=timing(),
    )
    selector = CatalogueSelector(
        fixture.store,
        _configuration(
            catalogue_config(fixture, mapping_ids=[mapping], identity_scope="bounded-cache")
        ),
    )
    evidence = selector.run()
    assert sorted(item["value"] for item in evidence["result"]) == ([] if duplicate else [100, 200])
    assert len(selector.identity_documents) == 1
    assert len(selector.identity_indexes) == 1


def test_catalogue_keeps_version_one_normalization_identity_and_frozen_bytes(
    fixture: Fixture,
) -> None:
    fixture.source("one", [{"amount_ore": 1255}])
    config = fixture.config()
    normalized = copy.deepcopy(config)
    normalized["cutoff"] = WEDNESDAY
    for key in ("import_ids", "mapping_ids", "verification_reports", "revisions"):
        normalized[key] = sorted(normalized[key])
    for key, value in normalized["scope"].items():
        normalized["scope"][key] = sorted(set(value))
    assert _configuration(config) == normalized
    expected_id = str(
        uuid5(
            NAMESPACE_URL,
            "bef-redbird/foundation/v1/selection/" + independent_hash(normalized),
        )
    )
    legacy = fixture.store.create_selection(config)
    assert legacy["selection_id"] == expected_id
    original_bytes = fixture.store._connection.execute(
        "SELECT configuration, evidence, evidence_hash FROM frozen_selection WHERE selection_id=?",
        [expected_id],
    ).fetchone()
    newer = fixture.store.create_selection(catalogue_config(fixture))
    assert newer["selection_id"] != expected_id
    assert newer["policy_version"] == legacy["policy_version"] == "f3-selection-v1"
    assert fixture.store.create_selection(config) == legacy
    assert (
        fixture.store._connection.execute(
            "SELECT configuration, evidence, evidence_hash FROM frozen_selection "
            "WHERE selection_id=?",
            [expected_id],
        ).fetchone()
        == original_bytes
    )


def test_catalogue_batched_readback_checks_later_batches(fixture: Fixture) -> None:
    fixture.source("many-candidates", [{"amount_ore": 100} for _ in range(501)])
    frozen = fixture.store.create_selection(catalogue_config(fixture))
    assert len(frozen["evidence"]["inputs"]["observations"]) == 501
    last = max(row["observation_id"] for row in fixture.rows)
    fixture.store._connection.execute(
        "UPDATE price_observation SET amount_ore=101 WHERE observation_id=?", [last]
    )
    with pytest.raises(FoundationError, match="Frozen source assertion changed"):
        fixture.store.inspect_selection(frozen["selection_id"])


def test_catalogue_readback_pins_entire_verification_archive(fixture: Fixture) -> None:
    fixture.source("two", [{"amount_ore": 100}, {"package_index": 1, "amount_ore": 200}])
    config = catalogue_config(fixture)
    frozen = fixture.store.create_selection(config)
    proof = fixture.store.original_path(config["verification_reports"][0])
    proof.write_bytes(proof.read_bytes() + b" ")
    with pytest.raises(FoundationError):
        fixture.store.inspect_selection(frozen["selection_id"])
