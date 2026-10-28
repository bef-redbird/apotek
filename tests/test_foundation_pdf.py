"""Independently specified synthetic printed catalogues; no private fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from bef_redbird.foundation import Store
from bef_redbird.foundation.adapters.base import AdapterError
from bef_redbird.foundation.adapters.medicine_pdf import (
    medicine_pdf_period,
    parse_medicine_pdf,
)

CONTEXT = {"current_period": "2020-01-06", "tax_basis": "unknown"}


def _put(
    page: Any, x: float, y: float, text: str, *, right: bool = False, size: float = 6.5
) -> None:
    if right:
        x -= pymupdf.get_text_length(text, fontname="helv", fontsize=size)
    page.insert_text((x, y), text, fontsize=size, fontname="helv")


def _page(document: Any, *, header: bool = False, footer: str = "06.01.2020") -> Any:
    page = document.new_page(width=595, height=842)
    _put(page, 35, 808, f"Priser for den {footer}")
    if header:
        _put(page, 441, 37, "ESP pr. enhed")
        _put(page, 511, 37, "ESP")
        _put(page, 548, 37, "TSP")
    return page


def _row(
    page: Any,
    y: float = 70,
    *,
    identifier: str = "000123",
    name: tuple[str, ...] = ("Example",),
    form: tuple[str, ...] = ("tablets",),
    unit: tuple[str, ...] = ("1,20",),
    package: tuple[str, ...] = ("120,00",),
    reimbursement: tuple[str, ...] = ("100,00",),
    company: tuple[str, ...] = ("Example maker",),
    abc: str = "B-pris",
    dose: bool = True,
    dispensing: str = "B",
    marker_x: float = 438.68,
    marker_size: float = 6.5,
    unknown: str | None = None,
) -> None:
    _put(page, 35, y, identifier)
    for x, fragments in (
        (82.96, name),
        (174.13, form),
        (253.76, ("10 mg",)),
        (340.66, ("100 stk.",)),
    ):
        for index, fragment in enumerate(fragments):
            _put(page, x, y + index * 8, fragment)
    for end, fragments in ((487.22, unit), (524.11, package), (561, reimbursement)):
        for index, fragment in enumerate(fragments):
            _put(page, end, y + index * 8, fragment, right=True)
    _put(page, marker_x, y, " U" if marker_x != 438.68 else "U", size=marker_size)
    detail_y = y + 10.75 + (max(len(name), len(form), len(unit), len(package)) - 1) * 8
    for index, fragment in enumerate(company):
        _put(page, 82.96, detail_y - 0.5 + index * 8, fragment)
    _put(page, 174.13, detail_y, "Below 25 C")
    _put(page, 438.68, detail_y, dispensing)
    if abc:
        _put(page, 485.02, detail_y, abc, right=True)
    if dose:
        _put(page, 554, detail_y - 0.5, "D")
    if unknown:
        _put(page, 300, detail_y, unknown)


def _catalogue(
    rows: list[dict[str, Any]] | None = None,
    *,
    continuation: bool = False,
    last_heading: str | None = None,
    last_header: bool = True,
    index: bool = True,
    footer: str | None = None,
    printed_date: str = "06.01.2020",
    guide: bool = True,
) -> bytes:
    with pymupdf.open() as document:
        cover = _page(document, footer=printed_date)
        _put(cover, 35, 50, "Medicinpriser")
        page = _page(document, footer=printed_date)
        _put(page, 35, 50, "LÆSEVEJLEDNING" if guide else "Unrelated material")
        _put(page, 35, 70, "ESP pr. enhed ESP TSP inkl. recepturgebyr")
        # The six-digit identifier in the guide is not a package assertion.
        _put(page, 35, 100, "999999")
        _page(document, footer=printed_date)
        body = _page(document, header=True, footer=footer or printed_date)
        _put(body, 35, 53.25, "A01AA01")
        for row in rows if rows is not None else [{}]:
            _row(body, **row)
        if last_heading:
            _put(body, 35, 760, last_heading)
        if continuation:
            body = _page(document, header=last_header, footer=printed_date)
            _row(body, identifier="000456", y=50)
        if index:
            page = _page(document, footer=printed_date)
            _put(page, 35, 50, "INDEKS")
            _put(page, 35, 70, "Navn ATC Side")
            _put(page, 35, 90, "888888")
        return document.tobytes()


def _prices(parsed: Any) -> dict[tuple[str, str], Any]:
    return {
        (assertion.values["price_type"], assertion.values["price_basis"]): assertion
        for assertion in parsed.assertions
        if assertion.table == "price_observation"
    }


def test_pdf_actual_cover_period_and_five_distinct_assertions() -> None:
    data = _catalogue()
    assert medicine_pdf_period(data) == date(2020, 1, 6)
    parsed = parse_medicine_pdf(data, CONTEXT)
    assert parsed.records == 1
    assert len(parsed.assertions) == 5
    assert parsed.issues == []
    assert {assertion.source_identifier for assertion in parsed.assertions} == {"000123"}
    assert {assertion.effective_period for assertion in parsed.assertions} == {date(2020, 1, 6)}
    package = parsed.assertions[0]
    assert package.values == {
        "name": "Example",
        "form": "tablets",
        "strength": "10 mg",
        "pack_description": "100 stk.",
        "company": "Example maker",
        "storage_code": "Below 25 C",
        "atc": "A01AA01",
    }
    prices = _prices(parsed)
    assert prices["consumer", "per_unit"].values["amount_ore"] == 120
    assert prices["consumer", "per_package"].values["amount_ore"] == 12000
    assert prices["reimbursement_basis", "per_package"].values["amount_ore"] == 10000
    assert all(assertion.values["tax_basis"] == "unknown" for assertion in prices.values())
    assert prices["consumer", "per_package"].values["fee_basis"] == (
        "includes_prescription_and_preparation_fees"
    )
    classification = parsed.assertions[-1]
    assert classification.values["classification_context"] == "ordinary"
    assert classification.values["abc_code"] == "B"
    assert classification.values["group_code"] is None
    assert classification.presence["group_code"] == "unreported"
    assert package.raw["source_columns"]["dose_dispensable"] == "D"


def test_pdf_wrapped_large_prices_and_attributes_are_reassembled_by_column() -> None:
    parsed = parse_medicine_pdf(
        _catalogue(
            [
                {
                    "unit": ("19.544.958,", "30"),
                    "package": ("19.544.95", "8,30"),
                    "name": ("Long product", "name"),
                    "form": ("injection,", "solution"),
                    "company": ("Long distributor", "name"),
                    "marker_x": 436.33,
                }
            ]
        ),
        CONTEXT,
    )
    assert parsed.issues == []
    prices = _prices(parsed)
    assert prices["consumer", "per_package"].values["amount_ore"] == 1954495830
    assert prices["consumer", "per_unit"].values["amount_ore"] == 1954495830
    assert prices["consumer", "per_package"].raw["amount_source"] == "19.544.958,30"
    assert len(prices["consumer", "per_package"].raw["amount_spans"]) == 2
    assert parsed.assertions[0].values["name"] == "Long product name"
    assert parsed.assertions[0].values["form"] == "injection, solution"
    assert parsed.assertions[0].values["company"] == "Long distributor name"
    assert parsed.assertions[0].raw["source_columns"]["reimbursement_marker"] == "U"


def test_pdf_consumer_tax_evidence_does_not_establish_reimbursement_tax() -> None:
    prices = _prices(
        parse_medicine_pdf(
            _catalogue(),
            {**CONTEXT, "tax_basis": "vat_included", "reimbursement_tax_basis": "unknown"},
        )
    )
    assert prices["consumer", "per_unit"].values["tax_basis"] == "vat_included"
    assert prices["consumer", "per_package"].values["tax_basis"] == "vat_included"
    assert prices["reimbursement_basis", "per_package"].values["tax_basis"] == "unknown"
    assert prices["consumer", "per_package"].values["amount_ore"] == 12000
    assert prices["reimbursement_basis", "per_package"].values["amount_ore"] == 10000


@pytest.mark.parametrize("basis", ["", None, 1])
def test_pdf_invalid_reimbursement_tax_context_rejects(basis: Any) -> None:
    with pytest.raises(AdapterError, match="missing_price_context"):
        parse_medicine_pdf(_catalogue(), {**CONTEXT, "reimbursement_tax_basis": basis})


@pytest.mark.parametrize(
    ("token", "state", "code", "amount"),
    [
        ("0,00", "present", None, 0),
        ("-,--", "unreported", None, None),
        ("1,234", "invalid", "unsupported_money_precision", None),
        ("12.34,50", "invalid", "malformed_money", None),
        ("-1,00", "invalid", "negative_money", None),
    ],
)
def test_pdf_money_missingness_and_exact_precision(
    token: str, state: str, code: str | None, amount: int | None
) -> None:
    parsed = parse_medicine_pdf(_catalogue([{"unit": (token,)}]), CONTEXT)
    prices = _prices(parsed)
    unit = prices["consumer", "per_unit"]
    assert unit.values["amount_ore"] == amount
    assert unit.presence == {"amount_ore": state}
    assert unit.raw["amount_source"] == token
    assert prices["consumer", "per_package"].values["amount_ore"] == 12000
    assert [issue.code for issue in parsed.issues] == ([code] if code else [])
    if code:
        assert parsed.issues[0].source_locator == "pdf:page:4:record:1:ESP_per_unit"
        assert parsed.issues[0].identifier == "000123"
        assert parsed.issues[0].blocking_scope == "field"


def test_pdf_missing_field_is_different_from_no_fixed_price() -> None:
    parsed = parse_medicine_pdf(_catalogue([{"unit": (), "package": ("-,--",)}]), CONTEXT)
    prices = _prices(parsed)
    assert prices["consumer", "per_unit"].presence["amount_ore"] == "missing"
    assert prices["consumer", "per_package"].presence["amount_ore"] == "unreported"
    assert [issue.code for issue in parsed.issues] == ["missing_pdf_price"]


@pytest.mark.parametrize("heading", [None, "QA12AX"])
def test_pdf_atc_heading_continues_across_page_without_invented_group(heading: str | None) -> None:
    parsed = parse_medicine_pdf(
        _catalogue(continuation=True, last_heading=heading, last_header=False), CONTEXT
    )
    packages = [a for a in parsed.assertions if a.table == "package_observation"]
    assert len(packages) == 2
    assert packages[1].values["atc"] == (heading or "A01AA01")
    assert packages[1].raw["atc_heading"]["source_locator"] == "pdf:page:4"
    assert packages[1].source_locator == "pdf:page:5:record:1"
    assert all(
        a.values["group_code"] is None
        for a in parsed.assertions
        if a.table == "classification_observation"
    )


def test_pdf_duplicate_identifiers_remain_separate_physical_assertions() -> None:
    parsed = parse_medicine_pdf(_catalogue([{}, {"y": 110, "package": ("121,00",)}]), CONTEXT)
    assert parsed.records == 2
    prices = [
        a.values["amount_ore"]
        for a in parsed.assertions
        if a.table == "price_observation"
        and a.values["price_type"] == "consumer"
        and a.values["price_basis"] == "per_package"
    ]
    assert prices == [12000, 12100]
    assert {
        a.source_locator.rsplit(":", 1)[-1]
        for a in parsed.assertions
        if a.table == "package_observation"
    } == {"1", "2"}


def test_pdf_unknown_fields_and_raw_coordinates_remain_recoverable() -> None:
    parsed = parse_medicine_pdf(
        _catalogue([{"unknown": "Supplementary source field", "abc": "", "dose": False}]), CONTEXT
    )
    package = parsed.assertions[0]
    refs = package.raw["unmapped_spans"]
    unknown = [span for span in package.raw["source_spans"] if span[3] in refs]
    assert len(unknown) == 1
    assert unknown[0][0] == "Supplementary source field"
    assert unknown[0][2][0] == 300
    assert len(unknown[0][1]) == 4
    classification = parsed.assertions[-1]
    assert classification.presence["abc_code"] == "blank"
    assert classification.values["abc_code"] is None
    assert package.raw["source_columns"]["dose_dispensable"] == ""


def test_pdf_bad_identifier_is_accounted_for_without_emitting_guessed_id() -> None:
    parsed = parse_medicine_pdf(_catalogue([{"identifier": "00A123"}]), CONTEXT)
    assert parsed.records == 1
    assert parsed.assertions == []
    assert len(parsed.issues) == 1
    assert parsed.issues[0].code == "invalid_identifier"
    assert parsed.issues[0].blocking_scope == "row"


def test_pdf_different_symbol_font_height_does_not_join_adjacent_records() -> None:
    parsed = parse_medicine_pdf(
        _catalogue([{}, {"y": 93.5, "identifier": "000456", "marker_size": 9}]), CONTEXT
    )
    assert parsed.records == 2
    assert parsed.issues == []
    assert [
        a.raw["source_columns"]["reimbursement_marker"]
        for a in parsed.assertions
        if a.table == "package_observation"
    ] == ["U", "U"]


def test_pdf_invalid_classification_is_scoped_without_discarding_prices() -> None:
    parsed = parse_medicine_pdf(_catalogue([{"abc": "X-pris"}]), CONTEXT)
    assert parsed.assertions[-1].presence["abc_code"] == "invalid"
    assert parsed.assertions[-1].values["abc_code"] is None
    assert len(_prices(parsed)) == 3
    assert [issue.code for issue in parsed.issues] == ["invalid_abc_code"]


def test_pdf_unresolvable_record_bands_reject_the_candidate_explicitly() -> None:
    parsed = parse_medicine_pdf(_catalogue([{"dispensing": ""}]), CONTEXT)
    assert parsed.records == 1
    assert parsed.assertions == []
    assert len(parsed.issues) == 1
    assert parsed.issues[0].code == "ambiguous_pdf_record_bands"
    assert parsed.issues[0].blocking_scope == "row"


@pytest.mark.parametrize("period", [{}, {"current_period": None}])
def test_pdf_printed_date_does_not_establish_tariff_period(period: dict[str, Any]) -> None:
    data = _catalogue([{}, {"y": 110, "identifier": "000456"}], printed_date="03.01.2020")
    assert medicine_pdf_period(data) == date(2020, 1, 3)
    parsed = parse_medicine_pdf(data, {"tax_basis": "unknown", **period})
    assert parsed.records == 2
    assert len(parsed.assertions) == 10
    assert all(assertion.effective_period is None for assertion in parsed.assertions)
    assert all(assertion.report_period is None for assertion in parsed.assertions)
    assert all(assertion.effective_date is None for assertion in parsed.assertions)
    assert all(assertion.quality_state == "incomplete" for assertion in parsed.assertions)
    assert all(
        assertion.raw["printed_price_date"] == "2020-01-03" for assertion in parsed.assertions
    )
    assert all("tariff_date" not in assertion.raw for assertion in parsed.assertions)
    assert len(parsed.issues) == 1
    warning = parsed.issues[0]
    assert warning.code == "unresolved_pdf_tariff_period"
    assert warning.source_locator == "pdf:page:1"
    assert warning.blocking_scope == "none"
    assert warning.field == "effective_period"
    assert warning.severity == "warning"
    assert warning.period is None


def test_pdf_undated_warning_persists_with_no_dated_assertions(tmp_path: Path) -> None:
    source = tmp_path / "unknown-period.pdf"
    source.write_bytes(_catalogue(printed_date="03.01.2020"))
    with Store(tmp_path / "foundation.duckdb", tmp_path / "originals") as store:
        delivery = store.register_source(source, source="synthetic", receipt_key="one")
        result = store.ingest(
            delivery,
            adapter="medicine-prices",
            version="medicine-prices-pdf-v1",
            mapping_version="synthetic-v1",
            mapping_policy="delivery",
            context={"tax_basis": "unknown"},
        )
        assert result["validation_status"] == "accepted"
        assert result["accepted_count"] == 5
        for table in ("package_observation", "price_observation", "classification_observation"):
            assert all(
                row["effective_period"] is None
                for row in store.observations(result["import_id"], table)
            )
        issues = store.issues(result["import_id"])
        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert issues[0]["blocking_scope"] == "none"


def test_pdf_undated_invalid_price_keeps_invalid_quality_and_exact_source() -> None:
    parsed = parse_medicine_pdf(
        _catalogue([{"unit": ("1,234",)}], printed_date="03.01.2020"),
        {"tax_basis": "unknown"},
    )
    unit = _prices(parsed)["consumer", "per_unit"]
    assert unit.quality_state == "invalid"
    assert unit.raw["amount_source"] == "1,234"
    assert unit.effective_period is None
    assert {issue.code for issue in parsed.issues} == {
        "unresolved_pdf_tariff_period",
        "unsupported_money_precision",
    }


def test_pdf_undated_source_still_rejects_conflicting_footer_dates() -> None:
    with pytest.raises(AdapterError, match="declared_observed_context_conflict"):
        parse_medicine_pdf(
            _catalogue(printed_date="03.01.2020", footer="06.01.2020"),
            {"tax_basis": "unknown"},
        )


def test_pdf_declared_period_cannot_replace_different_printed_date() -> None:
    with pytest.raises(AdapterError, match="declared_observed_context_conflict"):
        parse_medicine_pdf(_catalogue(printed_date="03.01.2020"), CONTEXT)


@pytest.mark.parametrize(
    ("data", "context", "code"),
    [
        (b"not a PDF", CONTEXT, "unsupported_source_format"),
        (
            _catalogue(),
            {"current_period": "2020-01-20", "tax_basis": "unknown"},
            "declared_observed_context_conflict",
        ),
        (_catalogue(footer="20.01.2020"), CONTEXT, "declared_observed_context_conflict"),
        (_catalogue(guide=False), CONTEXT, "unsupported_pdf_guide"),
        (_catalogue(), {"current_period": "2020-01-06"}, "missing_price_context"),
        (
            _catalogue(continuation=True, last_header=False, index=False),
            CONTEXT,
            "unsupported_pdf_page",
        ),
    ],
)
def test_pdf_context_and_layout_failures_are_explicit(
    data: bytes, context: dict[str, Any], code: str
) -> None:
    with pytest.raises(AdapterError, match=code):
        parse_medicine_pdf(data, context)
