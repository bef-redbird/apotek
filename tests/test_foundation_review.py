"""Independent adversarial persistence cases with invented workbook evidence."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from bef_redbird.foundation import Store

CURRENT = date(2025, 1, 20)
TARGET = date(2025, 2, 3)
CITO_CONTEXT = {
    "price_type": "aip",
    "currency": "DKK",
    "price_basis": "per_package",
    "tax_basis": "unknown",
    "fee_basis": "unknown",
    "classification_context": "ordinary",
    "group_namespace": "invented-group",
}
HISTORY_CONTEXT = {"tax_basis": "unknown", "fee_basis": "unknown"}


def _source(tmp_path: Path, name: str, sheet: str, rows: list[list[Any]]) -> Path:
    book = Workbook()
    target = book.active
    assert target is not None
    target.title = sheet
    for row in rows:
        target.append(row)
    path = tmp_path / name
    book.save(path)
    book.close()
    return path


def _periods(store: Store) -> None:
    for period in (date(2025, 1, 6), CURRENT, TARGET):
        store.add_period(period, period + timedelta(days=14))


def _cito(tmp_path: Path, name: str = "invented.xlsx") -> Path:
    return _source(
        tmp_path,
        name,
        "Table",
        [
            ["varenr", "opbevkode", "kostpris", "subkode", "nysubkode", "subgrp"],
            [7, "", 1, "A", "C", "123"],
        ],
    )


def _receipt(store: Store, path: Path, key: str) -> str:
    return store.register_source(
        path,
        source="synthetic",
        receipt_key=key,
        current_period=CURRENT,
        target_period=TARGET,
        coverage={"version": 1, "kind": "partial_extract"},
    )


def _ingest_cito(store: Store, delivery: str, **kwargs: Any) -> dict[str, Any]:
    return store.ingest(
        delivery,
        adapter="cito",
        version="cito-workbook-v1",
        mapping_version="review-v1",
        context=CITO_CONTEXT,
        **kwargs,
    )


def test_delivery_mapping_scope_cannot_be_applied_to_a_different_delivery(tmp_path: Path) -> None:
    path = _cito(tmp_path)
    with Store(tmp_path / "review.duckdb", tmp_path / "archive") as store:
        _periods(store)
        first_delivery = _receipt(store, path, "first")
        first = _ingest_cito(store, first_delivery, mapping_policy="delivery")
        first_package = store.observations(first["import_id"], "price_observation")[0]["package_id"]
        reused = _ingest_cito(
            store,
            first_delivery,
            mapping_policy="existing",
            mapping_scope=f"delivery:{first_delivery}",
        )
        assert reused["validation_status"] == "accepted"
        assert (
            store.observations(reused["import_id"], "price_observation")[0]["package_id"]
            == first_package
        )
        second_delivery = _receipt(store, path, "second")
        # The same bytes can be received in distinct identity contexts. A scope
        # explicitly limited to the first delivery establishes no mapping for the second.
        with pytest.raises(ValueError, match="scope"):
            _ingest_cito(
                store,
                second_delivery,
                mapping_policy="existing",
                mapping_scope=f"delivery:{first_delivery}",
            )
        assert not store._connection.execute(
            "SELECT 1 FROM price_observation WHERE delivery_id = ? AND package_id = ?",
            [second_delivery, first_package],
        ).fetchall()


def test_uuid_spelling_preserves_import_package_and_reserved_scope_identity(tmp_path: Path) -> None:
    with Store(tmp_path / "review.duckdb", tmp_path / "archive") as store:
        _periods(store)
        delivery = _receipt(store, _cito(tmp_path), "uuid-spelling")
        first = _ingest_cito(store, delivery, mapping_policy="delivery")
        repeated = _ingest_cito(store, delivery.upper(), mapping_policy="delivery")
        assert repeated == first
        original = store.observations(first["import_id"], "price_observation")[0]
        changed = store.ingest(
            delivery.upper(),
            adapter="cito",
            version="cito-workbook-v1",
            mapping_version="review-v1",
            mapping_policy="delivery",
            context={**CITO_CONTEXT, "tax_basis": "included"},
        )
        assert changed["import_id"] != first["import_id"]
        alternate = store.observations(changed["import_id"], "price_observation")[0]
        assert alternate["delivery_id"] == original["delivery_id"]
        assert alternate["package_id"] == original["package_id"]
        canonical_scope = _ingest_cito(
            store,
            delivery,
            mapping_scope=f"delivery:{delivery}",
        )
        uppercase_scope = _ingest_cito(
            store,
            delivery.upper(),
            mapping_scope=f"delivery:{delivery.upper()}",
        )
        assert uppercase_scope == canonical_scope
        assert (
            store.observations(canonical_scope["import_id"], "price_observation")[0]["package_id"]
            == original["package_id"]
        )
        assert store._connection.execute("SELECT count(*) FROM package").fetchone() == (1,)
        assert store._connection.execute(
            "SELECT DISTINCT scope_key FROM package_identifier"
        ).fetchall() == [(f"delivery:{delivery}",)]


def test_publication_failure_rolls_back_mapping_issues_and_assertions_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Store(tmp_path / "review.duckdb", tmp_path / "archive") as store:
        _periods(store)
        delivery = _receipt(store, _cito(tmp_path), "first")
        original_insert = store._insert

        def interrupted_insert(table: str, record: dict[str, Any]) -> None:
            if table == "package_observation":
                raise RuntimeError("simulated interruption after price publication")
            original_insert(table, record)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_insert", interrupted_insert)
            with pytest.raises(RuntimeError, match="simulated interruption"):
                _ingest_cito(store, delivery, mapping_policy="delivery")
        imports = store._connection.execute(
            "SELECT import_id, validation_status FROM import_batch"
        ).fetchall()
        assert len(imports) == 1 and imports[0][1] == "running"
        for table in (
            "price_observation",
            "package_observation",
            "classification_observation",
            "package_identifier",
            "package",
            "import_issue",
        ):
            assert store._connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        retried = _ingest_cito(store, delivery, mapping_policy="delivery")
        assert retried["import_id"] == str(imports[0][0])
        assert retried["accepted_count"] == 4
        assert retried["validation_status"] == "accepted"
        repeated = _ingest_cito(store, delivery, mapping_policy="delivery")
        assert repeated == retried
        assert store._connection.execute("SELECT count(*) FROM package_identifier").fetchone() == (
            1,
        )


def test_sparse_attribute_conflict_does_not_mark_unreported_fields_conflicting(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "history.xlsx",
        "Medicinpriser data",
        [
            ["Varenummer", "Indikator", "Firma", "Lægemiddel", "20250120"],
            [7, "AIP", "invented company one", "same invented name", 1],
            [7, "AIP", "invented company two", "same invented name", 1],
        ],
    )
    with Store(tmp_path / "review.duckdb", tmp_path / "archive") as store:
        _periods(store)
        delivery = _receipt(store, source, "history")
        batch = store.ingest(
            delivery,
            adapter="lms-history",
            version="lms-history-workbook-v1",
            mapping_version="review-v1",
            mapping_policy="delivery",
            context=HISTORY_CONTEXT,
        )
        packages = store.observations(batch["import_id"], "package_observation")
        assert len(packages) == 2
        for package in packages:
            presence = json.loads(package["field_presence"])
            assert presence["company"] == "conflicting"
            assert presence["name"] == "present"
            assert presence["form"] == "unreported"
        prices = store.observations(batch["import_id"], "price_observation")
        assert len(prices) == 2
        assert {row["quality_state"] for row in prices} == {"valid"}
        assert {row["amount_ore"] for row in prices} == {100}
        assert {issue["field"] for issue in store.issues(batch["import_id"])} == {"company"}


def test_unknown_history_attribute_date_cannot_borrow_report_or_price_date_for_interval_mapping(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "history.xlsx",
        "Medicinpriser data",
        [
            ["Varenummer", "Indikator", "Firma", "20250120"],
            [7, "AIP", "invented current company", 1],
        ],
    )
    with Store(tmp_path / "review.duckdb", tmp_path / "archive") as store:
        _periods(store)
        seed_delivery = _receipt(store, _cito(tmp_path), "seed")
        seed = _ingest_cito(store, seed_delivery, mapping_policy="delivery")
        package = str(store.observations(seed["import_id"], "price_observation")[0]["package_id"])
        store.add_identifier(
            package,
            namespace="dk:varenummer",
            identifier_value="000007",
            evidence_import_id=seed["import_id"],
            source_locator="sheet:Table!A2",
            mapping_version="interval-v1",
            validity_kind="interval",
            valid_from=date(2025, 1, 6),
            valid_to=TARGET,
        )
        delivery = store.register_source(
            source,
            source="synthetic",
            receipt_key="unknown-current",
            coverage={"version": 1, "kind": "partial_extract"},
        )
        batch = store.ingest(
            delivery,
            adapter="lms-history",
            version="lms-history-workbook-v1",
            mapping_version="interval-v1",
            context=HISTORY_CONTEXT,
        )
        assert batch["accepted_count"] == 1
        assert batch["rejected_count"] == 1
        assert len(store.observations(batch["import_id"], "price_observation")) == 1
        assert store.observations(batch["import_id"], "package_observation") == []
        assert {issue["code"] for issue in store.issues(batch["import_id"])} == {
            "unknown_attribute_period",
            "unresolved_mapping",
        }


def test_new_accepted_mapping_creates_alternative_derivation_without_mutating_rejection(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "review.duckdb", tmp_path / "archive") as store:
        _periods(store)
        delivery = _receipt(store, _cito(tmp_path), "mapping-revision")
        rejected = _ingest_cito(store, delivery, mapping_scope=f"delivery:{delivery}")
        assert rejected["validation_status"] == "rejected"
        rejected_record = store.get_record("import_batch", rejected["import_id"])
        assert json.loads(rejected_record["interpretation_context"])["mapping_snapshot"] == []
        # Accepted source evidence now establishes a mapping, but the completed
        # rejected derivation still records the absence of that mapping at interpretation.
        evidence = _ingest_cito(store, delivery, mapping_policy="delivery")
        established = _ingest_cito(
            store,
            delivery,
            mapping_scope=f"delivery:{delivery}",
        )
        assert established["validation_status"] == "accepted"
        assert established["import_id"] != rejected["import_id"]
        established_record = store.get_record("import_batch", established["import_id"])
        snapshot = json.loads(established_record["interpretation_context"])["mapping_snapshot"]
        assert len(snapshot) == 1
        evidence_mapping = store.observations(evidence["import_id"], "price_observation")[0][
            "mapping_id"
        ]
        assert snapshot == [str(evidence_mapping)]
        assert store.get_record("import_batch", rejected["import_id"]) == rejected_record
        assert store.import_summary(rejected["import_id"]) == rejected
