"""Real report bytes through archive, adapter, validation and typed publication."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from openpyxl import Workbook

from bef_redbird.foundation import Store, ingestion
from tests.test_foundation_reports import (
    STOP,
    SUPPLY,
    changes_rows,
    intro_rows,
    supply_xls,
    workbook,
)

CURRENT = date(2026, 1, 5)
NEXT = date(2026, 1, 19)
PRICE_CONTEXT: dict[str, Any] = {"tax_basis": "unknown", "fee_basis": "unknown"}
SUPPLY_CONTEXT: dict[str, Any] = {
    "reported_status": "reported_disruption",
    "wholesaler_namespace": "synthetic:wholesaler",
    "date_basis": "source_effective_date",
}


def register(
    store: Store,
    tmp_path: Path,
    data: bytes,
    *,
    key: str = "one",
    current: date | None = CURRENT,
    coverage: dict[str, Any] | None = None,
    name: str = "misleading-19900101.xlsx",
) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    if current is not None:
        store.add_period(current, current + timedelta(days=14))
    return store.register_source(
        path,
        source="synthetic-reports",
        receipt_key=key,
        current_period=current,
        coverage=coverage or {"version": 1, "kind": "changes"},
    )


def ingest(
    store: Store,
    delivery: str,
    adapter: str,
    context: dict[str, Any],
    **options: Any,
) -> dict[str, Any]:
    return store.ingest(
        delivery,
        adapter=adapter,
        version=f"{adapter}-workbook-v1",
        mapping_version="synthetic-map-v1",
        context=context,
        **({"mapping_policy": "delivery"} | options),
    )


def test_four_real_report_families_publish_all_five_tables_with_provenance(tmp_path: Path) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        sources = [
            ("price-changes", workbook(changes_rows()), PRICE_CONTEXT),
            ("introductions", workbook(intro_rows()), PRICE_CONTEXT),
            (
                "discontinued",
                workbook(
                    [
                        STOP,
                        [
                            "Holder",
                            "Medicine",
                            "tablet",
                            "5 mg",
                            "000123",
                            "20 stk",
                            datetime(2020, 2, 3),
                            datetime(2026, 1, 5),
                        ],
                    ]
                ),
                {},
            ),
            (
                "supply",
                supply_xls(),
                {
                    **SUPPLY_CONTEXT,
                    "classification_context": "ordinary",
                    "group_namespace": "synthetic:groups",
                },
            ),
        ]
        expected = [
            {"price_observation": 4, "package_observation": 1},
            {"price_observation": 2, "package_observation": 1, "lifecycle_event": 1},
            {"package_observation": 1, "lifecycle_event": 2},
            {"package_observation": 1, "classification_observation": 1, "supply_observation": 1},
        ]
        imports = []
        for index, (adapter, data, context) in enumerate(sources):
            delivery = register(store, tmp_path, data, key=str(index))
            summary = ingest(store, delivery, adapter, context)
            assert summary["validation_status"] == "accepted"
            assert {table: count for table, count in summary["observations"].items() if count} == (
                expected[index]
            )
            imports.append(summary["import_id"])
            assert store.original_path(delivery).read_bytes() == data
            for table, count in expected[index].items():
                rows = store.observations(summary["import_id"], table)
                assert len(rows) == count
                for row in rows:
                    assert str(row["import_id"]) == summary["import_id"]
                    assert str(row["delivery_id"]) == delivery
                    assert row["canonical_identifier"] == "000123"
                    mapping = store.get_record("package_identifier", str(row["mapping_id"]))
                    assert str(mapping["package_id"]) == str(row["package_id"])
                    assert row["source_locator"].startswith("sheet:Synthetic!")
        prices = store.observations(imports[0], "price_observation")
        assert [
            (row["price_type"], row["amount_ore"], row["price_role"], row["effective_period"])
            for row in prices
        ] == [
            ("aip", 123450, "effective_price", CURRENT),
            ("registered", 140025, "effective_price", CURRENT),
            ("aip", 1001, "reported_previous_price", None),
            ("registered", 1230, "reported_previous_price", None),
        ]
        assert all(row["report_period"] == CURRENT for row in prices)
        assert json.loads(prices[0]["source_values"])["Ny AIP"] == "1.234,50"
        introduction = store.observations(imports[1], "lifecycle_event")[0]
        assert introduction["event_type"] == "introduction"
        assert introduction["effective_date"] == CURRENT
        assert introduction["interpretation_status"] == "established"
        intro_prices = store.observations(imports[1], "price_observation")
        assert sorted(row["amount_ore"] for row in intro_prices) == [0, 2005]


def test_malformed_price_publishes_scoped_gap_and_valid_sibling_prices_atomically(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, tmp_path, workbook(changes_rows(new="12,345")))
        summary = ingest(store, delivery, "price-changes", PRICE_CONTEXT)
        assert summary["validation_status"] == "accepted"
        assert summary["coverage_assessment"] == "incomplete"
        assert summary["accepted_count"] == 5
        rows = store.observations(summary["import_id"], "price_observation")
        assert [row["amount_ore"] for row in rows] == [None, 140025, 1001, 1230]
        assert rows[0]["quality_state"] == "invalid"
        assert json.loads(rows[0]["field_presence"])["amount_ore"] == "invalid"
        assert json.loads(rows[0]["source_values"])["Ny AIP"] == "12,345"
        issues = store.issues(summary["import_id"])
        assert [
            (i["code"], i["source_locator"], i["field"], i["blocking_scope"]) for i in issues
        ] == [
            ("unsupported_money_precision", "sheet:Synthetic!G5", "amount_ore", "field"),
        ]
        assert ingest(store, delivery, "price-changes", PRICE_CONTEXT) == summary
        assert store.issues(summary["import_id"]) == issues


@pytest.mark.parametrize(
    "adapter,rows",
    [
        ("price-changes", changes_rows()),
        ("introductions", intro_rows()),
    ],
)
def test_observed_tariff_context_conflict_rejects_every_table_and_mapping(
    tmp_path: Path,
    adapter: str,
    rows: list[list[Any]],
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, tmp_path, workbook(rows), current=NEXT)
        summary = ingest(store, delivery, adapter, PRICE_CONTEXT)
        assert summary["validation_status"] == "rejected"
        assert sum(summary["observations"].values()) == 0
        assert [(i["code"], i["blocking_scope"]) for i in store.issues(summary["import_id"])] == [
            ("declared_observed_context_conflict", "import"),
        ]
        issue = store.issues(summary["import_id"])[0]
        assert issue["source_locator"] == (
            "sheet:Synthetic!E1" if adapter == "price-changes" else "sheet:Synthetic!J2"
        )
        assert store._connection.execute("SELECT count(*) FROM package").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM package_identifier").fetchone() == (
            0,
        )
        assert ingest(store, delivery, adapter, PRICE_CONTEXT) == summary


@pytest.mark.parametrize(
    "adapter,rows",
    [
        ("price-changes", changes_rows()),
        ("introductions", intro_rows()),
    ],
)
def test_report_filename_cannot_supply_missing_delivery_context(
    tmp_path: Path,
    adapter: str,
    rows: list[list[Any]],
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(
            store, tmp_path, workbook(rows), current=None, name="period-20260105.xlsx"
        )
        summary = ingest(store, delivery, adapter, PRICE_CONTEXT)
        assert summary["validation_status"] == "rejected"
        assert sum(summary["observations"].values()) == 0
        assert [i["code"] for i in store.issues(summary["import_id"])] == [
            "invalid_current_period_context",
        ]


def test_unknown_declared_period_is_rejected_by_delivery_calendar_foreign_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic.xlsx"
    path.write_bytes(workbook(changes_rows()))
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        with pytest.raises(duckdb.ConstraintException):
            store.register_source(
                path, source="synthetic", receipt_key="one", current_period=CURRENT
            )
        assert store._connection.execute("SELECT count(*) FROM source_delivery").fetchone() == (0,)
        assert store._connection.execute("SELECT count(*) FROM import_batch").fetchone() == (0,)


def test_unknown_calendar_date_in_real_history_is_scoped_without_backdating_attributes(
    tmp_path: Path,
) -> None:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Medicinpriser data"
    sheet.append(["Varenummer", "Indikator", "Firma", "20200106", "20260105"])
    sheet.append(["000123", "AIP", "Current holder", "5,00", "7,00"])
    output = io.BytesIO()
    book.save(output)
    book.close()
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, tmp_path, output.getvalue())
        summary = ingest(store, delivery, "lms-history", PRICE_CONTEXT)
        assert summary["validation_status"] == "accepted"
        assert summary["accepted_count"] == 2
        assert summary["rejected_count"] == 1
        assert summary["coverage_assessment"] == "incomplete"
        price = store.observations(summary["import_id"], "price_observation")
        assert [(row["effective_period"], row["amount_ore"]) for row in price] == [(CURRENT, 700)]
        package = store.observations(summary["import_id"], "package_observation")[0]
        assert package["effective_period"] == CURRENT
        assert package["company"] == "Current holder"
        assert [i["code"] for i in store.issues(summary["import_id"])] == [
            "unknown_calendar_period"
        ]
        assert store._connection.execute("SELECT period_start FROM price_period").fetchall() == [
            (CURRENT,),
        ]


def test_lifecycle_historical_stop_later_deletion_and_revisions_remain_immutable(
    tmp_path: Path,
) -> None:
    rows: list[list[Any]] = [
        STOP,
        [
            "Holder",
            "Medicine",
            "tablet",
            "5 mg",
            "000123",
            "20 stk",
            datetime(2020, 2, 3),
            datetime(2026, 1, 5),
        ],
    ]
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, tmp_path, workbook(rows))
        first = ingest(store, delivery, "discontinued", {})
        events = store.observations(first["import_id"], "lifecycle_event")
        assert [(row["event_type"], row["effective_date"]) for row in events] == [
            ("deactivation", date(2020, 2, 3)),
            ("permanent_deletion", CURRENT),
        ]
        assert all(row["interpretation_status"] == "unknown" for row in events)
        assert {row["original_source_code"] for row in events} == set(STOP[6:])
        assert len({row["observation_id"] for row in events}) == 2
        assert {i["code"] for i in store.issues(first["import_id"])} == {
            "source_reported_event_date_semantics_unknown",
        }
        rows[-1][-1] = datetime(2026, 1, 19)
        correction = register(store, tmp_path, workbook(rows), key="correction")
        second = ingest(store, correction, "discontinued", {})
        assert store.observations(first["import_id"], "lifecycle_event") == events
        revised = store.observations(second["import_id"], "lifecycle_event")
        assert [row["effective_date"] for row in revised] == [date(2020, 2, 3), NEXT]


def test_missing_stop_date_does_not_drop_independently_dated_deletion(tmp_path: Path) -> None:
    rows: list[list[Any]] = [
        STOP,
        [
            "Holder",
            "Medicine",
            "tablet",
            "5 mg",
            "000123",
            "20 stk",
            None,
            CURRENT,
        ],
    ]
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        summary = ingest(store, register(store, tmp_path, workbook(rows)), "discontinued", {})
        assert summary["validation_status"] == "accepted"
        events = store.observations(summary["import_id"], "lifecycle_event")
        assert [(row["event_type"], row["effective_date"]) for row in events] == [
            ("permanent_deletion", CURRENT),
        ]
        assert {i["code"] for i in store.issues(summary["import_id"])} == {
            "missing_event_date",
            "source_reported_event_date_semantics_unknown",
        }


def test_supply_dated_deliveries_remain_assertions_and_omission_cannot_create_recovery(
    tmp_path: Path,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        data = supply_xls()
        delivery = register(store, tmp_path, data, current=None)
        package = store.create_package()
        evidence = store.begin_import(
            delivery,
            parser_version="synthetic-identity-v1",
            schema_version="synthetic-v1",
            mapping_version="synthetic-map-v1",
        )
        store.add_identifier(
            package,
            namespace="dk:varenummer",
            identifier_value="000123",
            evidence_import_id=evidence,
            source_locator="synthetic:independent-identity",
            mapping_version="synthetic-map-v1",
            validity_kind="scope",
            scope_key="synthetic:recurring-supplier-reports",
        )
        store.finish_import(
            evidence, status="accepted", record_count=0, accepted_count=0, rejected_count=0
        )
        mapping = {
            "mapping_policy": "existing",
            "mapping_scope": "synthetic:recurring-supplier-reports",
        }
        imports = []
        for index, day in enumerate(("2026-01-06", "2026-01-07")):
            receipt = (
                delivery if index == 0 else register(store, tmp_path, data, key="two", current=None)
            )
            summary = ingest(
                store,
                receipt,
                "supply",
                {
                    "reported_status": "reported_disruption",
                    "wholesaler_namespace": "synthetic",
                    "observation_date": day,
                    "observation_date_basis": "synthetic report evidence",
                },
                **mapping,
            )
            imports.append(summary["import_id"])
        reports = [store.observations(batch, "supply_observation")[0] for batch in imports]
        assert [row["observation_date"] for row in reports] == [date(2026, 1, 6), date(2026, 1, 7)]
        assert all(str(row["package_id"]) == package for row in reports)
        assert all(row["effective_date"] == CURRENT for row in reports)
        assert all(row["report_period"] is None for row in reports)
        assert all(row["reported_status"] == "reported_disruption" for row in reports)
        assert len({row["observation_id"] for row in reports}) == 2
        empty = register(store, tmp_path, workbook([SUPPLY]), key="empty", current=None)
        omitted = ingest(store, empty, "supply", SUPPLY_CONTEXT, **mapping)
        assert omitted["observations"]["supply_observation"] == 0
        assert store._connection.execute("SELECT count(*) FROM supply_observation").fetchone() == (
            2,
        )
        assert [store.observations(batch, "supply_observation")[0] for batch in imports] == reports


def test_real_price_report_publication_failure_rolls_back_issues_and_rows_then_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, tmp_path, workbook(changes_rows(new="invalid,")))
        publish: Callable[..., Any] = ingestion._publish

        def fail_after_second(*args: Any, **kwargs: Any) -> None:
            publish(*args, **kwargs)
            table = args[3].table
            if table == "price_observation":
                raise RuntimeError("synthetic interruption after scoped issue and second assertion")

        with monkeypatch.context() as patch:
            patch.setattr(ingestion, "_publish", fail_after_second)
            with pytest.raises(RuntimeError, match="synthetic interruption"):
                ingest(store, delivery, "price-changes", PRICE_CONTEXT)
        for table in (*ingestion.TABLE_FIELDS, "import_issue", "package_identifier", "package"):
            assert store._connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        record = store._connection.execute("SELECT import_id FROM import_batch").fetchone()
        assert record is not None
        batch = str(record[0])
        assert store.get_record("import_batch", batch)["validation_status"] == "running"
        summary = ingest(store, delivery, "price-changes", PRICE_CONTEXT)
        assert summary["import_id"] == batch
        assert summary["accepted_count"] == 5
        assert len(store.issues(batch)) == 1


@pytest.mark.parametrize("wholesaler", [None, "=1+1"])
def test_supply_missing_required_wholesaler_is_a_scoped_gap_not_a_database_failure(
    tmp_path: Path,
    wholesaler: Any,
) -> None:
    rows = [
        SUPPLY,
        [
            "000123",
            "Medicine",
            "tablet",
            "5 mg",
            "20 stk",
            "Holder",
            "01",
            "C",
            wholesaler,
            CURRENT,
        ],
    ]
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, tmp_path, workbook(rows), current=None)
        summary = ingest(store, delivery, "supply", SUPPLY_CONTEXT)
        assert summary["validation_status"] == "accepted"
        assert summary["observations"]["package_observation"] == 1
        assert summary["observations"]["supply_observation"] == 0
        assert summary["coverage_assessment"] == "incomplete"
        assert any(
            i["field"] == "wholesaler_identifier" for i in store.issues(summary["import_id"])
        )


@pytest.mark.parametrize("invalid_date", ["formula", "invalid_date"])
def test_source_effective_date_failure_keeps_durable_issues_and_valid_package_only(
    tmp_path: Path,
    invalid_date: str,
) -> None:
    if invalid_date == "formula":
        data = supply_xls(formula=True)
        expected_code = "formula_cell"
    else:
        data = workbook(
            [
                SUPPLY,
                [
                    "000123",
                    "Medicine",
                    "tablet",
                    "5 mg",
                    "20 stk",
                    "Holder",
                    "01",
                    "C",
                    "Wholesaler",
                    "31-02-2026",
                ],
            ]
        )
        expected_code = "invalid_date"
    with Store(tmp_path / "store.duckdb", tmp_path / "archive") as store:
        delivery = register(store, tmp_path, data, current=None)
        summary = ingest(store, delivery, "supply", SUPPLY_CONTEXT)
        assert summary["validation_status"] == "accepted"
        assert summary["observations"]["package_observation"] == 1
        assert summary["observations"]["supply_observation"] == 0
        assert summary["coverage_assessment"] == "incomplete"
        issues = store.issues(summary["import_id"])
        assert {(i["code"], i["field"], i["blocking_scope"], i["severity"]) for i in issues} == {
            (expected_code, "effective_date", "field", "error"),
            ("missing_supply_observation_date", "observation_date", "field", "error"),
        }
        assert all(issue["source_locator"] == "sheet:Synthetic!J2" for issue in issues)
        assert store.original_path(delivery).read_bytes() == data
        assert ingest(store, delivery, "supply", SUPPLY_CONTEXT) == summary
        assert store.issues(summary["import_id"]) == issues
