"""Independent synthetic expectations for source-specific report semantics."""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any
from zipfile import ZipFile

import pytest
import xlwt  # type: ignore[import-untyped]
from openpyxl import Workbook

from bef_redbird.foundation.adapters.base import AdapterError
from bef_redbird.foundation.adapters.reports import (
    parse_discontinued,
    parse_introductions,
    parse_price_changes,
    parse_supply,
)

CHANGES = [
    "Firma",
    "Navn",
    "Form",
    "Styrke",
    "Vnr",
    "Pakningstekst",
    "Ny AIP",
    "Ny reg. pris",
    "Gl. AIP",
    "Gl. reg. pris",
    "Prisforskel %",
]
INTRO = [
    "Navn",
    "Form",
    "Styrke",
    "MF-indehaver",
    "Pakningsejer",
    "Varenr",
    "Pakningsstørrelse",
    "Opbevaringsbetingelse",
    "Udlevering",
    "Trafikadvarsel DK",
    "AIP (Kr.)",
    "Reg. pris",
]
STOP = [
    "Repræsentant",
    "Navn",
    "Form",
    "Styrke",
    "VareNr",
    "RPk",
    "Stop (Deaktiveret)",
    "Slet (Udgår permanent)",
]
SUPPLY = [
    "Varenr.",
    "Lægemiddel",
    "Form",
    "Styrke",
    "Pakningsstr.",
    "Firma",
    "Substitutionsgrp.nr.",
    "ABC-markering",
    "Grossist",
    "Ikrafttrædelsesdato",
]
PRICE_CONTEXT = {
    "current_period": "2026-01-05",
    "tax_basis": "unknown",
    "fee_basis": "unknown",
}
SUPPLY_CONTEXT = {
    "observation_date": "2026-01-06",
    "wholesaler_namespace": "synthetic-wholesaler",
    "reported_status": "reported_disruption",
    "observation_date_basis": "synthetic independently established report date",
}


def workbook(rows: list[list[Any]], *, duplicate: bool = False) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Synthetic"
    for row in rows:
        sheet.append(row)
    if duplicate:
        book.copy_worksheet(sheet)
    out = io.BytesIO()
    book.save(out)
    book.close()
    return out.getvalue()


def changes_rows(*, tariff: str = "05-01-2026", new: Any = "1.234,50") -> list[list[Any]]:
    metadata: list[Any] = [None] * 12
    metadata[0] = "Prisændringer"
    metadata[4] = f"Takstdato: {tariff}"
    metadata[11] = "Kørselsdato: 02-01-2026"
    return [
        metadata,
        [],
        [],
        CHANGES.copy(),
        [
            "Holder",
            "Medicine",
            "tablet",
            "5 mg",
            "000123",
            "20 stk",
            new,
            1400.25,
            "10,01",
            "12,30",
            "4,9",
        ],
    ]


def intro_rows(*, header_row: int = 5, punctuation: bool = False) -> list[list[Any]]:
    header = INTRO.copy()
    if punctuation:
        header[5] = "Varenr."
    metadata: list[Any] = [None] * 10
    metadata[9] = "Takstdato: 05-01-2026"
    return [
        ["Nye pakninger på eksisterende formuleringer"],
        metadata,
        *([[]] * (header_row - 3)),
        header,
        [
            "Medicine",
            "tablet",
            "5 mg",
            "Licence holder",
            "Pack owner",
            "000123",
            "20 stk",
            "KO",
            "B",
            "Nej",
            "0,00",
            "20,05",
        ],
    ]


def test_changes_preserve_price_types_previous_unknown_period_raw_and_attributes() -> None:
    result = parse_price_changes(workbook(changes_rows()), PRICE_CONTEXT)
    assert result.records == 1
    assert result.issues == []
    prices = [a for a in result.assertions if a.table == "price_observation"]
    assert [
        (a.values["price_type"], a.values["amount_ore"], a.values["price_role"]) for a in prices
    ] == [
        ("aip", 123450, "effective_price"),
        ("registered", 140025, "effective_price"),
        ("aip", 1001, "reported_previous_price"),
        ("registered", 1230, "reported_previous_price"),
    ]
    assert [a.effective_period for a in prices] == [date(2026, 1, 5)] * 2 + [None] * 2
    assert all(a.report_period == date(2026, 1, 5) for a in result.assertions)
    assert all(a.source_identifier == "000123" for a in result.assertions)
    assert prices[0].raw["Ny AIP"] == "1.234,50"
    assert prices[0].raw["Prisforskel %"] == "4,9"
    assert prices[0].raw["run_date"] == "2026-01-02"
    package = next(a for a in result.assertions if a.table == "package_observation")
    assert package.effective_period == date(2026, 1, 5)
    assert package.presence["atc"] == "unreported"
    assert package.source_locator == "sheet:Synthetic!row:5"
    assert prices[0].source_locator == "sheet:Synthetic!G5"


@pytest.mark.parametrize(
    "bad,code",
    [
        ("12,345", "unsupported_money_precision"),
        ("12,3x", "malformed_money"),
        (-1, "negative_money"),
        ("=1+1", "formula_cell"),
    ],
)
def test_bad_price_is_scoped_and_does_not_round_or_drop_other_values(bad: Any, code: str) -> None:
    result = parse_price_changes(workbook(changes_rows(new=bad)), PRICE_CONTEXT)
    assert [(i.code, i.field, i.source_locator) for i in result.issues] == [
        (code, "amount_ore", "sheet:Synthetic!G5"),
    ]
    prices = [a for a in result.assertions if a.table == "price_observation"]
    assert prices[0].values["amount_ore"] is None
    assert prices[0].presence == {"amount_ore": "invalid"}
    assert prices[0].raw["Ny AIP"] == (bad if isinstance(bad, str) else str(bad))
    assert [a.values["amount_ore"] for a in prices[1:]] == [140025, 1001, 1230]


def test_blank_price_keeps_blank_distinct_from_zero() -> None:
    result = parse_price_changes(workbook(changes_rows(new=None)), PRICE_CONTEXT)
    price = next(a for a in result.assertions if a.table == "price_observation")
    assert price.presence == {"amount_ore": "blank"}
    assert price.values["amount_ore"] is None
    assert not result.issues


@pytest.mark.parametrize(
    "parser,rows",
    [
        (parse_price_changes, changes_rows()),
        (parse_introductions, intro_rows()),
    ],
)
def test_declared_and_observed_tariff_conflict_is_fatal(parser: Any, rows: list[list[Any]]) -> None:
    with pytest.raises(AdapterError, match="declared_observed_context_conflict"):
        parser(workbook(rows), {**PRICE_CONTEXT, "current_period": "2026-01-19"})


@pytest.mark.parametrize("header_row,punctuation", [(5, False), (6, True)])
def test_introduction_title_tariff_establish_date_and_two_distinct_prices(
    header_row: int,
    punctuation: bool,
) -> None:
    result = parse_introductions(
        workbook(intro_rows(header_row=header_row, punctuation=punctuation)),
        PRICE_CONTEXT,
    )
    assert result.records == 1
    assert not result.issues
    event = next(a for a in result.assertions if a.table == "lifecycle_event")
    assert event.values == {
        "event_type": "introduction",
        "original_source_code": "Nye pakninger på eksisterende formuleringer",
        "interpretation_status": "established",
    }
    assert event.effective_date == date(2026, 1, 5)
    prices = [a for a in result.assertions if a.table == "price_observation"]
    assert [(a.values["price_type"], a.values["amount_ore"]) for a in prices] == [
        ("aip", 0),
        ("registered", 2005),
    ]
    package = next(a for a in result.assertions if a.table == "package_observation")
    assert package.values["company"] == "Pack owner"
    assert package.raw["MF-indehaver"] == "Licence holder"
    assert package.values["storage_code"] == "KO"


def test_missing_tariff_cannot_use_filename_or_current_declaration_as_date_evidence() -> None:
    rows = intro_rows()
    rows[1] = []
    with pytest.raises(AdapterError, match="missing_report_date"):
        parse_introductions(workbook(rows), {**PRICE_CONTEXT, "filename": "pak2602.xlsx"})


def test_stop_and_later_deletion_remain_separate_source_reported_dates() -> None:
    rows: list[list[Any]] = [
        STOP,
        [
            "Holder",
            "Medicine",
            "tablet",
            "5 mg",
            123,
            "20 stk",
            datetime(2020, 2, 3),
            datetime(2026, 1, 5),
        ],
    ]
    result = parse_discontinued(workbook(rows), {"current_period": "2026-01-05"})
    events = [a for a in result.assertions if a.table == "lifecycle_event"]
    assert [(a.values["event_type"], a.effective_date) for a in events] == [
        ("deactivation", date(2020, 2, 3)),
        ("permanent_deletion", date(2026, 1, 5)),
    ]
    assert [a.values["original_source_code"] for a in events] == STOP[6:]
    assert all(a.values["interpretation_status"] == "unknown" for a in events)
    assert all(a.source_identifier == "123" for a in events)
    assert events[0].raw["Stop (Deaktiveret)"] == "2020-02-03T00:00:00"
    assert all(i.severity == "warning" for i in result.issues)
    package = next(a for a in result.assertions if a.table == "package_observation")
    assert package.effective_period == date(2026, 1, 5)


def test_blank_and_invalid_event_dates_skip_only_affected_events() -> None:
    rows: list[list[Any]] = [
        STOP,
        ["Holder", "Medicine", "tablet", "5 mg", "000123", "20 stk", None, "not-a-date"],
    ]
    result = parse_discontinued(workbook(rows), {})
    assert not [a for a in result.assertions if a.table == "lifecycle_event"]
    assert [(i.code, i.source_locator) for i in result.issues] == [
        ("missing_event_date", "sheet:Synthetic!G2"),
        ("invalid_date", "sheet:Synthetic!H2"),
    ]


def test_discontinued_accepts_evidenced_newline_and_excel_escape_header_variants() -> None:
    header = STOP.copy()
    header[6] = "Stop_x000D_\n(Deaktiveret)"
    header[7] = "Slet\n(Udgår permanent)"
    result = parse_discontinued(
        workbook([header, [None] * 4 + ["000123", "20 stk", None, "05-01-2026"]]), {}
    )
    event = next(a for a in result.assertions if a.table == "lifecycle_event")
    assert event.values["original_source_code"] == "Slet\n(Udgår permanent)"
    assert event.effective_date == date(2026, 1, 5)


def supply_xls(*, formula: bool = False) -> bytes:
    book = xlwt.Workbook()
    sheet = book.add_sheet("Synthetic")
    for col, value in enumerate(SUPPLY):
        sheet.write(0, col, value)
    values: list[Any] = [
        123,
        "Medicine",
        "tablet",
        "5 mg",
        "20 stk",
        "Holder",
        "01",
        "C",
        "Wholesaler",
        datetime(2026, 1, 5),
    ]
    for col, value in enumerate(values):
        if col == 9:
            sheet.write(
                1,
                col,
                xlwt.Formula("1+1") if formula else value,
                xlwt.easyxf(num_format_str="DD-MM-YYYY"),
            )
        else:
            sheet.write(1, col, value)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_supply_biff_preserves_effective_date_serial_and_independent_observation_date() -> None:
    result = parse_supply(supply_xls(), SUPPLY_CONTEXT)
    assert result.records == 1
    assert result.issues == []
    assert not [a for a in result.assertions if a.table == "classification_observation"]
    supply = next(a for a in result.assertions if a.table == "supply_observation")
    assert supply.values == {
        "observation_date": date(2026, 1, 6),
        "wholesaler_namespace": "synthetic-wholesaler",
        "wholesaler_identifier": "Wholesaler",
        "reported_status": "reported_disruption",
    }
    assert supply.source_identifier == "123.0"
    assert supply.effective_date == date(2026, 1, 5)
    assert supply.raw["Ikrafttrædelsesdato"] == "46027.0"
    assert supply.report_period is None
    package = next(a for a in result.assertions if a.table == "package_observation")
    assert package.effective_period is None
    assert package.report_period is None


def test_supply_biff_formula_effective_date_is_not_cached_value() -> None:
    result = parse_supply(supply_xls(formula=True), SUPPLY_CONTEXT)
    supply = next(a for a in result.assertions if a.table == "supply_observation")
    assert supply.effective_date is None
    assert supply.presence["effective_date"] == "invalid"
    assert [(i.code, i.field) for i in result.issues] == [("formula_cell", "effective_date")]


@pytest.mark.parametrize(
    "key",
    [
        "observation_date",
        "observation_date_basis",
        "wholesaler_namespace",
        "reported_status",
    ],
)
def test_supply_requires_explicit_evidenced_context(key: str) -> None:
    context = {k: v for k, v in SUPPLY_CONTEXT.items() if k != key}
    with pytest.raises(AdapterError):
        parse_supply(supply_xls(), context)


def test_supply_empty_report_has_no_recovery_or_synthetic_zero_assertions() -> None:
    result = parse_supply(workbook([SUPPLY]), SUPPLY_CONTEXT)
    assert result.records == 0
    assert result.assertions == []
    assert result.issues == []


def test_supply_source_effective_date_can_date_assertion_without_guessing_report_date() -> None:
    result = parse_supply(
        supply_xls(),
        {
            "date_basis": "source_effective_date",
            "wholesaler_namespace": "synthetic",
            "reported_status": "reported_disruption",
        },
    )
    supply = next(a for a in result.assertions if a.table == "supply_observation")
    assert supply.values["observation_date"] == date(2026, 1, 5)
    assert supply.effective_date == date(2026, 1, 5)
    assert supply.raw["observation_date_role"] == "source_effective_date"
    assert supply.report_period is None
    assert not result.issues


def test_supply_invalid_source_date_cannot_supply_required_observation_date() -> None:
    result = parse_supply(
        supply_xls(formula=True),
        {
            "date_basis": "source_effective_date",
            "wholesaler_namespace": "synthetic",
            "reported_status": "reported_disruption",
        },
    )
    assert not [a for a in result.assertions if a.table == "supply_observation"]
    assert [i.code for i in result.issues] == ["formula_cell", "missing_supply_observation_date"]


def test_supply_classification_requires_period_and_namespace_scope() -> None:
    result = parse_supply(
        supply_xls(),
        {
            **SUPPLY_CONTEXT,
            "current_period": "2026-01-05",
            "group_namespace": "synthetic",
            "classification_context": "ordinary",
        },
    )
    classification = next(a for a in result.assertions if a.table == "classification_observation")
    assert classification.values == {
        "classification_context": "ordinary",
        "abc_code": "C",
        "group_namespace": "synthetic",
        "group_code": "01",
        "group_scope": "2026-01-05",
    }
    assert classification.effective_period == date(2026, 1, 5)


@pytest.mark.parametrize(
    "parser,context",
    [
        (parse_price_changes, PRICE_CONTEXT),
        (parse_introductions, PRICE_CONTEXT),
        (parse_discontinued, {}),
        (parse_supply, SUPPLY_CONTEXT),
    ],
)
@pytest.mark.parametrize("data", [b"{\\rtf1 unknown}", b"PK truncated", b"%PDF-1.4"])
def test_unknown_or_truncated_formats_are_fatal(parser: Any, context: Any, data: bytes) -> None:
    with pytest.raises(AdapterError):
        parser(data, context)


def test_multiple_matching_report_sheets_are_fatal() -> None:
    with pytest.raises(AdapterError, match="ambiguous_report_sheet"):
        parse_price_changes(workbook(changes_rows(), duplicate=True), PRICE_CONTEXT)


def test_no_guessed_header_or_unrecognized_title() -> None:
    rows = changes_rows()
    rows[3][6] = "Probably price"
    with pytest.raises(AdapterError, match="unsupported_schema"):
        parse_price_changes(workbook(rows), PRICE_CONTEXT)
    rows = intro_rows()
    rows[0] = ["Unknown introduction list"]
    with pytest.raises(AdapterError, match="unsupported_schema"):
        parse_introductions(workbook(rows), PRICE_CONTEXT)


def test_formula_identifier_is_scoped_record_failure() -> None:
    rows = changes_rows()
    rows[-1][4] = "=123"
    result = parse_price_changes(workbook(rows), PRICE_CONTEXT)
    assert result.records == 1
    assert result.assertions == []
    assert [(i.code, i.source_locator) for i in result.issues] == [
        ("invalid_identifier_cell", "sheet:Synthetic!E5"),
    ]


@pytest.mark.parametrize("bad", ["Takstdato: 31-02-2026", "=DATE(2026,1,5)"])
def test_invalid_or_formula_metadata_is_fatal(bad: str) -> None:
    rows = changes_rows()
    rows[0][4] = bad
    with pytest.raises(AdapterError, match="invalid_report_metadata"):
        parse_price_changes(workbook(rows), PRICE_CONTEXT)


def test_duplicate_different_observed_tariff_dates_are_fatal() -> None:
    rows = changes_rows()
    rows[0][5] = "Takstdato: 19-01-2026"
    with pytest.raises(AdapterError, match="declared_observed_context_conflict"):
        parse_price_changes(workbook(rows), PRICE_CONTEXT)


def test_macro_content_is_rejected_before_reading_rows() -> None:
    out = io.BytesIO()
    with ZipFile(io.BytesIO(workbook(changes_rows()))) as source, ZipFile(out, "w") as target:
        for entry in source.infolist():
            target.writestr(entry, source.read(entry.filename))
        target.writestr("xl/vbaProject.bin", b"synthetic macro marker")
    with pytest.raises(AdapterError, match="invalid_report_workbook"):
        parse_price_changes(out.getvalue(), PRICE_CONTEXT)


def test_nonidentifier_and_formula_attribute_preserve_source_field_states() -> None:
    rows = changes_rows()
    rows[-1][1] = "=1+1"
    result = parse_price_changes(workbook(rows), PRICE_CONTEXT)
    package = next(a for a in result.assertions if a.table == "package_observation")
    assert package.values["name"] is None
    assert package.presence["name"] == "invalid"
    assert package.raw["Navn"] == "=1+1"
    assert package.raw["source_cell_types"]["Navn"] == "f"
    assert package.raw["source_field_presence"]["Navn"] == "invalid"
    rows[-1][4] = "1234567"
    result = parse_price_changes(workbook(rows), PRICE_CONTEXT)
    assert result.records == 1
    assert not result.assertions
    assert [i.code for i in result.issues] == ["invalid_identifier"]
