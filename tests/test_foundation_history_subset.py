"""Bounded-v2 expectations retain untouched originals and original cell positions."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from bef_redbird.foundation import Store
from bef_redbird.foundation.adapters.base import AdapterError
from bef_redbird.foundation.adapters.workbooks import parse_history, parse_history_subset
from bef_redbird.foundation.source_audit import expected_history_subset, reconcile

CONTEXT: dict[str, Any] = {
    "tax_basis": "vat_excluded",
    "fee_basis": "dispensing_fees_excluded",
    "subset_identifiers": ["000001", "000002"],
    "subset_periods": ["2026-01-05", "2026-01-19"],
}


def source_bytes() -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Medicinpriser data"
    source_rows: list[list[Any]] = [
        [
            "Varenummer",
            "Indikator",
            "Lægemiddel",
            "Firma",
            "20251222",
            "20260105",
            "20260119",
            "20260202",
        ],
        ["000003", "AIP", "Out of scope", "Holder", 77, "malformed", "=1+1", 88],
        ["000001", "AIP", "Medicine", "Holder", 99, 0, "1,23", 777],
        ["000001", "Consumer", "Medicine", "Holder", 11, 22, 33, 44],
        ["bad-id", "AIP", "Unresolved", "Holder", 0, 0, 0, 0],
        ["000002", "AIP", None, "Holder", 100, "=1+1", None, 100],
    ]
    for row in source_rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def test_subset_filters_before_derivation_preserves_cell_locators_and_undated_attributes() -> None:
    parsed = parse_history_subset(source_bytes(), CONTEXT)
    assert parsed.records == 2
    assert len(parsed.assertions) == 6
    prices = [a for a in parsed.assertions if a.table == "price_observation"]
    assert [
        (a.source_identifier, a.source_locator, a.values["amount_ore"], a.presence["amount_ore"])
        for a in prices
    ] == [
        ("000001", "sheet:Medicinpriser data!F3", 0, "present"),
        ("000001", "sheet:Medicinpriser data!G3", 123, "present"),
        ("000002", "sheet:Medicinpriser data!F6", None, "invalid"),
        ("000002", "sheet:Medicinpriser data!G6", None, "blank"),
    ]
    attrs = [a for a in parsed.assertions if a.table == "package_observation"]
    assert all(a.effective_period is None and a.report_period is None for a in attrs)
    assert all(a.report_period is None for a in prices)
    assert prices[0].raw["date_heading"] == "20260105"
    assert prices[2].raw["price"] == "=1+1"
    assert [(i.code, i.source_locator) for i in parsed.issues] == [
        ("unknown_attribute_period", "sheet:Medicinpriser data!row:3"),
        ("formula_cell", "sheet:Medicinpriser data!F6"),
        ("unknown_attribute_period", "sheet:Medicinpriser data!row:6"),
    ]


def test_v1_retains_full_scope_even_when_subset_options_are_present() -> None:
    parsed = parse_history(source_bytes(), CONTEXT)
    assert parsed.records == 4
    assert len(parsed.assertions) == 15
    assert {a.effective_period for a in parsed.assertions if a.table == "price_observation"} == {
        date(2025, 12, 22),
        date(2026, 1, 5),
        date(2026, 1, 19),
        date(2026, 2, 2),
    }


@pytest.mark.parametrize(
    "key,value,code",
    [
        ("subset_identifiers", [], "invalid_history_subset_identifiers"),
        ("subset_identifiers", ["1"], "invalid_history_subset_identifiers"),
        ("subset_identifiers", ["bad-id"], "invalid_history_subset_identifiers"),
        ("subset_identifiers", ["000001", "000001"], "invalid_history_subset_identifiers"),
        ("subset_identifiers", [f"{i:06}" for i in range(6)], "invalid_history_subset_identifiers"),
        ("subset_periods", [], "invalid_history_subset_periods"),
        ("subset_periods", ["20260105"], "invalid_history_subset_periods"),
        ("subset_periods", ["2026-01-05"] * 2, "invalid_history_subset_periods"),
        (
            "subset_periods",
            ["2026-01-05", "2026-01-19", "2026-02-02"],
            "invalid_history_subset_periods",
        ),
        ("subset_periods", ["2026-03-02"], "missing_history_subset_period"),
    ],
)
def test_subset_scope_requires_exact_explicit_supported_keys(
    key: str, value: Any, code: str
) -> None:
    with pytest.raises(AdapterError, match=code):
        parse_history_subset(source_bytes(), {**CONTEXT, key: value})


def test_independent_expectations_account_every_row_and_unselected_date() -> None:
    expected = expected_history_subset(source_bytes(), CONTEXT)
    assert expected["scope_accounting"] == {
        "physical_data_rows": 5,
        "aip_rows": 4,
        "selected_aip_rows": 2,
        "unselected_aip_rows": 2,
        "non_aip_rows": 1,
        "invalid_unscoped_identifiers": 1,
        "selected_invalid_indicators": 0,
        "selected_price_cells": 4,
        "unselected_date_fields_per_selected_row": 2,
    }
    assert len(expected["assertions"]) == 6
    prices = [a for a in expected["assertions"] if a["table"] == "price_observation"]
    assert [a["values"]["amount_ore"] for a in prices] == [0, 123, None, None]
    assert [a["presence"]["amount_ore"] for a in prices] == [
        "present",
        "present",
        "invalid",
        "blank",
    ]
    assert [i["code"] for i in expected["issues"]] == [
        "unknown_attribute_period",
        "formula_cell",
        "unknown_attribute_period",
    ]


def test_bounded_v2_publication_and_independent_reconciliation(tmp_path: Path) -> None:
    database = tmp_path / "foundation.duckdb"
    original = tmp_path / "history.xlsx"
    original_bytes = source_bytes()
    original.write_bytes(original_bytes)
    with Store(database, tmp_path / "archive") as store:
        store.add_period(date(2026, 1, 5), date(2026, 1, 19))
        store.add_period(date(2026, 1, 19), date(2026, 2, 2))
        delivery = store.register_source(
            original,
            source="synthetic-history",
            receipt_key="one",
            coverage={
                "version": 1,
                "kind": "partial_extract",
                "family": "lms-history",
                "filter": {k: CONTEXT[k] for k in ("subset_identifiers", "subset_periods")},
            },
        )
        summary = store.ingest(
            delivery,
            adapter="lms-history",
            version="lms-history-workbook-v2",
            mapping_version="synthetic-v1",
            context=CONTEXT,
            mapping_policy="delivery",
        )
        assert summary["accepted_count"] == 6
    report = reconcile(database, tmp_path / "archive", [original])
    assert report["complete"]
    assert report["verified_count"] == 6
    assert report["imports"][0]["scope_accounting"]["unselected_aip_rows"] == 2
    assert original.read_bytes() == original_bytes
    assert len(report["observation_hashes"]) == 6
