"""Independently authored printed source recipes and audit expectations."""

from __future__ import annotations

from typing import Any

import pymupdf
import pytest

from bef_redbird.foundation.pdf_audit import expected_pdf

CONTEXT = {"current_period": "2020-01-06", "tax_basis": "unknown"}


def text(page: Any, x: float, y: float, value: str, *, right: bool = False) -> None:
    if value:
        if right:
            x -= pymupdf.get_text_length(value, fontsize=6.5, fontname="helv")
        page.insert_text((x, y), value, fontsize=6.5)


def add_page(document: Any, *, header: bool = False) -> Any:
    page = document.new_page(width=595, height=842)
    text(page, 35, 808, "Priser for den 06.01.2020")
    if len(document) > 1:
        text(page, 558, 824, str(len(document)), right=True)
    if header:
        for x, label in ((441, "ESP pr. enhed"), (511, "ESP"), (548, "TSP")):
            text(page, x, 37, label)
    return page


def add_record(
    page: Any,
    y: float = 70,
    *,
    identifier: str = "000012",
    abc: str = "A-pris",
    prices: tuple[tuple[str, ...], ...] = (("1,25",), ("25,00",), ("20,00",)),
    wrap: bool = False,
    unknown: bool = False,
    dispensing: str = "B",
    attributes: bool = True,
) -> None:
    text(page, 35, y, identifier)
    if attributes:
        for x, value in (
            (82.96, "Example"),
            (174.13, "tablet"),
            (253.76, "5 mg"),
            (340.66, "20 pcs"),
        ):
            text(page, x, y, value)
        if wrap:
            text(page, 82.96, y + 8, "long name")
            text(page, 174.13, y + 8, "coated")
    for edge, parts in zip((487.22, 524.11, 561), prices, strict=True):
        for line, token in enumerate(parts):
            text(page, edge, y + 8 * line, token, right=True)
    details = y + max(2 if wrap else 1, *(len(p) for p in prices)) * 8 + 3
    text(page, 438.68, y, "U")
    if attributes:
        text(page, 82.96, details - 0.5, "Maker")
        text(page, 174.13, details, "Cold")
        if wrap:
            text(page, 82.96, details + 7.5, "Limited")
    text(page, 438.68, details, dispensing)
    text(page, 435.68, details, "Specialist", right=True)
    text(page, 485.02, details, abc, right=True)
    text(page, 554, details - 0.5, "D")
    if unknown:
        text(page, 300, details, "unmapped source")


def catalogue(
    records: list[dict[str, Any]] | None = None,
    *,
    continuation: bool = False,
    trailing_heading: bool = False,
    heading: bool = True,
    index: bool = True,
    last_header: bool = False,
) -> bytes:
    with pymupdf.open() as document:
        cover = add_page(document)
        text(cover, 35, 50, "Medicinpriser")
        guide = add_page(document)
        text(guide, 35, 50, "LÆSEVEJLEDNING")
        text(guide, 35, 70, "ESP pr. enhed ESP TSP inkl. recepturgebyr")
        text(guide, 35, 100, "999999")
        add_page(document)
        page = add_page(document, header=True)
        if heading:
            text(page, 35, 53, "A01AA01")
            text(page, 82.96, 53, "Source heading")
        for recipe in records if records is not None else [{}]:
            add_record(page, **recipe)
        if trailing_heading:
            text(page, 35, 760, "QA12AX")
            text(page, 82.96, 760, "Next section")
        if continuation:
            page = add_page(document, header=last_header)
            add_record(page, y=50, identifier="000034")
        if index:
            page = add_page(document)
            text(page, 35, 50, "INDEKS")
            text(page, 35, 70, "Navn ATC Side")
            text(page, 35, 100, "888888")
        return document.tobytes()


def audit(data: bytes, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return expected_pdf(data, "medicine-prices", CONTEXT if context is None else context)


def test_all_assertions_exact_fields_and_original_provenance() -> None:
    result = audit(catalogue())
    package, unit, pack, reimbursement, classification = result["assertions"]
    assert result["source_rows"] == 1
    assert result["issues"] == []
    assert package["source_identifier"] == package["canonical_identifier"] == "000012"
    assert package["values"] == {
        "name": "Example",
        "form": "tablet",
        "strength": "5 mg",
        "pack_description": "20 pcs",
        "company": "Maker",
        "storage_code": "Cold",
        "atc": "A01AA01",
    }
    assert [item["values"]["amount_ore"] for item in (unit, pack, reimbursement)] == [
        125,
        2500,
        2000,
    ]
    assert unit["values"]["price_basis"] == "per_unit"
    assert pack["values"]["price_type"] == "consumer"
    assert reimbursement["values"]["price_type"] == "reimbursement_basis"
    assert all(
        a["effective_period"] == a["report_period"] == "2020-01-06" for a in result["assertions"]
    )
    assert classification["values"]["abc_code"] == "A"
    assert classification["values"]["classification_context"] == "ordinary"
    assert classification["presence"]["group_code"] == "unreported"
    assert package["raw"]["source_columns"]["prescribing_specialties"] == "Specialist"
    assert package["raw"]["source_columns"]["dose_dispensable"] == "D"
    assert unit["raw"]["amount_source"] == "1,25"
    assert unit["raw"]["amount_spans"][0][0] == "1,25"
    assert unit["raw"]["amount_spans"][0][1][2] == pytest.approx(487.22, abs=0.01)
    assert package["raw"]["atc_heading"]["source_locator"] == "pdf:page:4"
    assert unit["source_locator"] == "pdf:page:4:record:1:ESP_per_unit"
    assert package["raw"]["printed_price_date"] == "2020-01-06"
    assert [a["kind"] for a in result["physical_accounting"]] == [
        "cover",
        "reading_guide",
        "reading_guide",
        "medicine_table",
        "name_index",
    ]
    page = result["physical_accounting"][3]
    assert page["candidates"] == page["accepted_records"] == 1
    assert (
        page["native_spans"] == page["margin_spans"] + page["heading_spans"] + page["record_spans"]
    )


def test_consumer_tax_evidence_does_not_establish_reimbursement_tax() -> None:
    result = audit(
        catalogue(),
        {
            **CONTEXT,
            "tax_basis": "vat_included",
            "reimbursement_tax_basis": "unknown",
        },
    )
    prices = [a for a in result["assertions"] if a["table"] == "price_observation"]
    assert [a["values"]["tax_basis"] for a in prices] == [
        "vat_included",
        "vat_included",
        "unknown",
    ]
    assert prices[2]["raw"]["amount_column"] == "TSP"
    assert prices[2]["raw"]["amount_source"] == "20,00"
    assert result["issues"] == []
    for invalid in (None, ""):
        with pytest.raises(ValueError, match="missing_price_context"):
            audit(catalogue(), {**CONTEXT, "reimbursement_tax_basis": invalid})


def test_wrapped_main_and_secondary_bands_and_large_money() -> None:
    result = audit(
        catalogue(
            [
                {
                    "wrap": True,
                    "prices": (("19.544.958,", "30"), ("19.544.95", "8,30"), ("0,00",)),
                }
            ]
        )
    )
    package, unit, pack, reimbursement, _ = result["assertions"]
    assert result["issues"] == []
    assert package["values"]["name"] == "Example long name"
    assert package["values"]["form"] == "tablet coated"
    assert package["values"]["company"] == "Maker Limited"
    assert unit["values"]["amount_ore"] == pack["values"]["amount_ore"] == 1954495830
    assert reimbursement["values"]["amount_ore"] == 0
    assert pack["raw"]["amount_source"] == "19.544.958,30"
    assert len(pack["raw"]["amount_spans"]) == 2
    assert package["raw"]["source_columns"]["ESP"] == "19.544.95 8,30"


@pytest.mark.parametrize(
    ("token", "state", "expected", "code"),
    [
        ("0,00", "present", 0, None),
        ("-0,000", "present", 0, None),
        ("1,2300", "present", 123, None),
        ("-,--", "unreported", None, None),
        ("", "missing", None, "missing_pdf_price"),
        ("-0,01", "invalid", None, "negative_money"),
        ("12,345", "invalid", None, "unsupported_money_precision"),
        ("12.34,50", "invalid", None, "malformed_money"),
        ("1e6", "invalid", None, "malformed_money"),
        ("92233720368547758,08", "invalid", None, "money_overflow"),
    ],
)
def test_money_states_preserve_exact_source(
    token: str,
    state: str,
    expected: int | None,
    code: str | None,
) -> None:
    result = audit(catalogue([{"prices": ((token,), ("25,00",), ("20,00",))}]))
    unit = result["assertions"][1]
    assert unit["values"]["amount_ore"] == expected
    assert unit["presence"]["amount_ore"] == state
    assert unit["raw"]["amount_source"] == token
    assert [i["code"] for i in result["issues"]] == ([] if code is None else [code])
    if code:
        issue = result["issues"][0]
        assert issue["field"] == "amount_ore"
        assert issue["source_locator"] == "pdf:page:4:record:1:ESP_per_unit"
        assert issue["identifier"] == "000012"
        assert issue["blocking_scope"] == "field"


def test_undated_printed_report_cannot_invent_official_tariff_period() -> None:
    result = audit(catalogue(), {"tax_basis": "unknown"})
    assert len(result["assertions"]) == 5
    assert all(
        a["effective_period"] is a["report_period"] is a["effective_date"] is None
        for a in result["assertions"]
    )
    assert {a["quality"] for a in result["assertions"]} == {"incomplete"}
    assert result["issues"] == [
        {
            "source_locator": "pdf:page:1",
            "code": "unresolved_pdf_tariff_period",
            "identifier": None,
            "field": "effective_period",
            "period": None,
            "severity": "warning",
            "blocking_scope": "none",
        }
    ]
    invalid = audit(
        catalogue([{"abc": "Z-pris"}]), {"tax_basis": "unknown", "current_period": None}
    )
    assert invalid["assertions"][-1]["quality"] == "invalid"


@pytest.mark.parametrize("trailing", [False, True])
def test_heading_continues_across_page_and_final_header_can_be_absent(trailing: bool) -> None:
    result = audit(catalogue(continuation=True, trailing_heading=trailing))
    packages = [a for a in result["assertions"] if a["table"] == "package_observation"]
    assert result["source_rows"] == 2
    assert packages[1]["values"]["atc"] == ("QA12AX" if trailing else "A01AA01")
    assert packages[1]["raw"]["atc_heading"]["source_locator"] == "pdf:page:4"
    assert packages[1]["source_locator"] == "pdf:page:5:record:1"


def test_unreported_atc_blank_attributes_and_early_absent_abc_are_distinct() -> None:
    result = audit(catalogue([{"attributes": False, "abc": ""}], heading=False))
    package, *_, classification = result["assertions"]
    assert package["presence"]["atc"] == "unreported"
    assert package["presence"]["name"] == "blank"
    assert package["values"]["name"] is None
    assert classification["presence"]["abc_code"] == "blank"
    assert classification["quality"] == "valid"


def test_duplicate_records_and_unknown_native_spans_are_not_dropped() -> None:
    result = audit(catalogue([{"unknown": True}, {"y": 120}]))
    packages = [a for a in result["assertions"] if a["table"] == "package_observation"]
    assert len(packages) == 2
    assert result["metadata"]["duplicate_identifier_records"] == 1
    assert len(packages[0]["raw"]["unmapped_spans"]) == 1
    assert "unmapped source" in [s[0] for s in packages[0]["raw"]["source_spans"]]
    assert result["physical_accounting"][3]["unmapped_record_spans"] == 1


def test_invalid_identifier_is_accounted_as_rejected_physical_record() -> None:
    result = audit(catalogue([{"identifier": "ABC123"}]))
    assert result["source_rows"] == 1
    assert result["assertions"] == []
    assert result["physical_accounting"][3]["rejected_records"] == 1
    assert result["issues"][0]["code"] == "invalid_identifier"
    assert result["issues"][0]["blocking_scope"] == "row"


def test_missing_identifier_record_cannot_merge_silently_into_previous() -> None:
    result = audit(catalogue([{}, {"y": 120, "identifier": ""}]))
    assert result["assertions"] == []
    assert result["issues"][0]["code"] == "ambiguous_pdf_record_bands"
    assert result["physical_accounting"][3]["rejected_records"] == 1


def test_missing_identifier_record_cannot_disappear_into_heading_context() -> None:
    with pytest.raises(ValueError, match="unaccounted_heading_content"):
        audit(catalogue([{"identifier": ""}]))


def test_record_outside_table_geometry_is_not_discarded() -> None:
    with pymupdf.open(stream=catalogue(), filetype="pdf") as document:
        text(document[3], 35, 795, "123456")
        data = document.tobytes()
    with pytest.raises(ValueError, match="record_outside_body"):
        audit(data)


def test_abc_and_dispensing_anomalies_are_explicit() -> None:
    result = audit(catalogue([{"abc": "Z-pris", "dispensing": "ZZ"}]))
    assert [i["code"] for i in result["issues"]] == ["invalid_abc_code", "unknown_dispensing_code"]
    assert result["assertions"][-1]["quality"] == "invalid"
    assert result["assertions"][-1]["presence"]["abc_code"] == "invalid"


def test_date_conflict_and_family_mismatch_are_quarantined() -> None:
    with pytest.raises(ValueError, match="report_context_conflict"):
        audit(catalogue(), {**CONTEXT, "current_period": "2020-01-20"})
    with pytest.raises(ValueError, match="unsupported_family"):
        expected_pdf(catalogue(), "introductions", CONTEXT)


def test_deleted_page_is_detected_by_printed_sequence() -> None:
    with pymupdf.open(stream=catalogue(continuation=True), filetype="pdf") as document:
        document.delete_page(3)
        data = document.tobytes()
    with pytest.raises(ValueError, match="page_sequence"):
        audit(data)


def test_unclaimed_body_before_first_record_is_not_ignored() -> None:
    with pymupdf.open(stream=catalogue(), filetype="pdf") as document:
        text(document[3], 200, 45, "orphan source record")
        data = document.tobytes()
    with pytest.raises(ValueError, match="unaccounted_body"):
        audit(data)


def test_unknown_table_page_is_not_treated_as_empty_or_index() -> None:
    with pymupdf.open(stream=catalogue(), filetype="pdf") as document:
        page = add_page(document)
        text(page, 35, 50, "unexpected appendix")
        data = document.tobytes()
    with pytest.raises(ValueError, match="table_after_index"):
        audit(data)


def test_index_words_cannot_hide_a_table_page() -> None:
    with pymupdf.open(stream=catalogue(), filetype="pdf") as document:
        text(document[3], 35, 30, "INDEKS Navn ATC Side")
        data = document.tobytes()
    with pytest.raises(ValueError, match="mixed_index_and_table"):
        audit(data)


def test_body_without_index_needs_its_running_header() -> None:
    with pytest.raises(ValueError, match="unknown_page"):
        audit(catalogue(continuation=True, index=False))


def test_truncated_catalogue_without_index_boundary_is_quarantined() -> None:
    with pytest.raises(ValueError, match="missing_index_boundary"):
        audit(catalogue(index=False))


def test_mixed_native_text_and_image_body_cannot_hide_unread_records() -> None:
    with pymupdf.open(stream=catalogue(), filetype="pdf") as document:
        image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 2, 2), 0)
        image.clear_with(255)
        document[3].insert_image((40, 120, 400, 150), pixmap=image)
        data = document.tobytes()
    with pytest.raises(ValueError, match="nontext_body"):
        audit(data)
